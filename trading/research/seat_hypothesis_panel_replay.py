"""Run the frozen public-seat hypothesis through the shared paper Ledger.

This is an explicitly candidate-panel replay.  It uses only SH/SZ symbols with
complete StockDB bars in the small two-source checked panel, keeps the event
score independent of future labels, and marks the result as unvalidated.  It
does not promote the panel to a market universe or touch a persistent account,
broker or Tonghuashun state.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..rules import stock_board
from ..settings import ROOT


HYPOTHESIS_ID = "lhb_identifiable_buy_concentration_v1"
AVAILABILITY_HYPOTHESIS_ID = "lhb_identifiable_buy_concentration_availability_v1"
AVAILABILITY_SCHEMA = "seat_candidate_availability_evidence_v1"
STOCKDAY_HYPOTHESIS_ID = "lhb_identifiable_buy_concentration_stockday_turnover_v2"
STOCKDAY_AVAILABILITY_ID = "lhb_identifiable_buy_concentration_stockday_turnover_availability_v2"
STOCKDAY_AVAILABILITY_SCHEMA = "seat_candidate_availability_evidence_stockday_v2"
STOCKDAY_SCOPE = "existing_stock_board_ordinary_SH_SZ_prefix_proxy_not_PIT_or_dated_roster"


def _candidate_method(candidates, *, empty_method=None):
    """Only the original recipe and the explicitly frozen stock-day recipe."""
    fields = {"hypothesis_id", "snapshot_method_id"}
    if not fields.intersection(candidates.columns):
        if empty_method == STOCKDAY_HYPOTHESIS_ID:
            raise ValueError("Stock-day candidates need their explicit recipe columns even when empty")
        return HYPOTHESIS_ID  # Unchanged original CSVs have no recipe columns.
    if not fields <= set(candidates.columns):
        raise ValueError("Candidate snapshot identity must include both fixed method fields")
    if candidates.empty and empty_method in {HYPOTHESIS_ID, STOCKDAY_HYPOTHESIS_ID}:
        return empty_method
    ids = set(candidates.hypothesis_id) | set(candidates.snapshot_method_id)
    if len(ids) != 1 or not ids <= {HYPOTHESIS_ID, STOCKDAY_HYPOTHESIS_ID}:
        raise ValueError("Unknown or mixed public-seat snapshot recipe")
    return next(iter(ids))


def _availability_method(evidence):
    stockday = evidence.get("schema") == STOCKDAY_AVAILABILITY_SCHEMA
    base = STOCKDAY_HYPOTHESIS_ID if stockday else HYPOTHESIS_ID
    method = STOCKDAY_AVAILABILITY_ID if stockday else AVAILABILITY_HYPOTHESIS_ID
    if (evidence.get("schema") not in {AVAILABILITY_SCHEMA, STOCKDAY_AVAILABILITY_SCHEMA}
            or (stockday and (evidence.get("hypothesis_id") != base
                or evidence.get("snapshot_method_id") != base
                or evidence.get("source_scope") != STOCKDAY_SCOPE
                or evidence.get("selection_enabled") is not False
                or evidence.get("strategy_validated") is not False))
            or (not stockday and any(evidence.get(key, HYPOTHESIS_ID) != HYPOTHESIS_ID
                for key in ("hypothesis_id", "snapshot_method_id")))
            or evidence.get("execution_admitted") is not False
            or evidence.get("historical_publication_verified") is not False):
        raise ValueError("Unknown or conflicting fixed seat availability method")
    return base, method


def _now():
    return datetime.now(MARKET_TZ)


def _file_ref(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return {"file": str(path), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


def _day(value):
    if not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
        raise ValueError("Availability event/session dates must be canonical ISO dates")
    return value


def _clock(value, actual):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Availability evidence clock must be an offset-aware ISO string or null")
    clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if clock.utcoffset() is None or clock > actual:
        raise ValueError("Availability evidence has a naive or future clock")
    return clock.astimezone(MARKET_TZ)


def _availability(path, candidates_path, bars_path, candidates, dates, lag, symbols,
                  *, base_hypothesis_id=HYPOTHESIS_ID):
    """Verify saved source bytes/clocks, then schedule only known events.

    The clock declarations are not independent historical publication evidence.
    Unknown events stay in the audit; they do not veto unrelated known events.
    """
    actual = _now()
    if actual.utcoffset() is None:
        raise ValueError("Availability read clock must be offset-aware")
    path = Path(path).resolve()
    evidence = json.loads(path.read_text(encoding="utf-8-sig"))
    candidate_ref, bars_ref, evidence_ref = map(_file_ref, (candidates_path, bars_path, path))
    expected_base, method = _availability_method(evidence)
    stockday = expected_base == STOCKDAY_HYPOTHESIS_ID
    if (base_hypothesis_id != expected_base
            or str(Path(evidence["candidates_file"]).resolve()) != candidate_ref["file"]
            or evidence.get("candidates_sha256") != candidate_ref["sha256"]
            or not isinstance(evidence.get("records"), list)):
        raise ValueError("Availability evidence must bind this exact candidate CSV and explicit research boundaries")
    panel_ref = None
    if "panel_file" in evidence or "panel_sha256" in evidence:
        panel_ref = _file_ref(evidence["panel_file"])
        if panel_ref["sha256"] != evidence.get("panel_sha256"):
            raise ValueError("Saved source hypothesis panel changed")
    expected = {(row.trade_date, row.symbol) for row in candidates.itertuples(index=False)}
    known, source_refs = {}, {}
    protected = {candidate_ref["file"], bars_ref["file"], evidence_ref["file"]}
    archive = evidence.get("source_archive")
    if archive is not None:
        if not isinstance(archive, dict) or not isinstance(archive.get("file"), str):
            raise ValueError("Saved source archive must be an exact file/hash reference")
        reference = _file_ref(archive["file"])
        if reference["file"] in protected or reference["sha256"] != archive.get("sha256"):
            raise ValueError("Saved source archive changed or conflicts with derived input files")
        source_refs[reference["file"]] = reference
    elif stockday:
        raise ValueError("Stock-day availability requires its complete cache archive even for an empty batch")
    if "source_available_at" in evidence:
        if _clock(evidence["source_available_at"], actual) is None:
            raise ValueError("Snapshot source availability must be an actual aware clock")
    for item in evidence["records"]:
        if not isinstance(item, dict):
            raise ValueError("Each availability record must be an event/source object")
        key = (_day(item["event_date"]), item["symbol"])
        if key not in expected or key in known:
            raise ValueError("Availability evidence has an unknown or duplicate candidate event")
        source = str(Path(item["source_file"]).resolve())
        if stockday and source != str(Path(archive["file"]).resolve()):
            raise ValueError("Stock-day events must bind the complete saved cache archive")
        if source in protected:
            raise ValueError("Original disclosure source must be distinct from candidate, bar and sidecar artifacts")
        if source not in source_refs:
            source_refs[source] = _file_ref(source)
        reference = source_refs[source]
        if reference["sha256"] != item.get("source_sha256"):
            raise ValueError("Original disclosure source hash changed or conflicts across event records")
        retrieved, published = (_clock(item.get(field), actual) for field in ("retrieved_at", "published_at"))
        if key[0] > actual.astimezone(MARKET_TZ).date().isoformat():
            raise ValueError("Availability candidate event lies in the future")
        effective = max(t for t in (retrieved, published) if t is not None) if retrieved is not None else None
        known[key] = {"source": reference, "retrieved_at": item.get("retrieved_at"),
            "published_at": item.get("published_at"),
            "available_at": effective.isoformat() if effective is not None else None,
            "clock_basis": ("retrieved_at_unknown" if retrieved is None else
                "reported_published_after_retrieval_not_independently_verified" if published is not None and published > retrieved else
                "saved_retrieval_and_reported_publication_clocks_not_independently_verified" if published is not None else
                "system_first_observation_only_not_publisher_verification")}
    for day in dates:
        _day(day)
    signals, schedule = {}, []
    date_index = {day: index for index, day in enumerate(dates)}
    for item in candidates.itertuples(index=False):
        key = (_day(item.trade_date), item.symbol)
        evidence_row = known.get(key)
        row = {"event_date": item.trade_date, "symbol": item.symbol, "score": float(item.score),
            "evidence": evidence_row, "lag": lag, "earliest_lag_signal_day": None,
            "signal_day": None, "execution_day": None, "status": "unknown", "reason": "availability_record_missing"}
        event_index = date_index.get(item.trade_date)
        if event_index is not None and event_index + lag < len(dates):
            row["earliest_lag_signal_day"] = dates[event_index + lag]
        if not item.symbol.endswith((".SSE", ".SZSE")):
            row.update(status="outside_scope", reason="source_candidate_outside_existing_SH_SZ_feed_scope")
        elif evidence_row is None:
            pass
        elif evidence_row["available_at"] is None:
            row["reason"] = "retrieved_at_unknown"
        elif item.symbol not in symbols:
            row.update(status="outside_panel", reason="symbol_absent_from_complete_bar_panel")
        elif event_index is None:
            row.update(status="outside_panel", reason="event_date_outside_observed_calendar")
        elif row["earliest_lag_signal_day"] is None:
            row.update(status="outside_panel", reason="lag_has_no_observed_decision_session")
        else:
            effective = datetime.fromisoformat(evidence_row["available_at"])
            eligible = [day for day in dates[event_index + lag:]
                if effective <= datetime.fromisoformat(day + "T15:00:00+08:00")]
            if not eligible:
                row.update(status="outside_panel", reason="available_after_last_observed_decision")
            elif eligible[0] == dates[-1]:
                row.update(status="withheld", reason="no_next_session_for_execution",
                    first_available_observed_decision_day=eligible[0])
            else:
                day = eligible[0]
                row.update(status="scheduled", reason="known_source_available_by_observed_close",
                    signal_day=day, execution_day=dates[date_index[day] + 1],
                    derived_signal_available_at=day + "T15:00:00+08:00")
                signals.setdefault(day, []).append({"symbol": item.symbol, "score": float(item.score),
                    "event_date_ordinal": float(pd.Timestamp(item.trade_date).toordinal())})
        schedule.append(row)
    # Separate events for the same security may become available together. Do
    # not invent a score aggregation or silently choose an event after the fact.
    for day, rows in signals.items():
        if len({row["symbol"] for row in rows}) != len(rows):
            raise ValueError(f"Two saved events for one symbol mature on the same decision session: {day}")
        rows.sort(key=lambda row: (-row["score"], row["symbol"]))
    proof = {"schema": evidence["schema"], "read_at": actual.isoformat(),
        "method_id": method, "base_hypothesis_id": expected_base, "snapshot_method_id": expected_base,
        "evidence_file": evidence_ref, "candidates_file": candidate_ref, "bars_file": bars_ref,
        "source_files": list(source_refs.values()), "event_rows": len(schedule),
        "known_clock_events": sum(row["evidence"] is not None and row["evidence"]["available_at"] is not None for row in schedule),
        "unknown_clock_events": sum(row["status"] == "unknown" for row in schedule),
        "scheduled_events": sum(row["status"] == "scheduled" for row in schedule),
        "outside_scope_events": sum(row["status"] == "outside_scope" for row in schedule),
        "scope": "known_available_events_only_not_complete_disclosure_candidate_batch",
        "unknown_event_is_not_a_veto_on_other_known_events": True,
        "decision_source_cutoff": "observed_session_15:00_Asia_Shanghai",
        "derived_signal_clock_is_not_original_publication_clock": True,
        "historical_publication_verified": False, "selection_enabled": False,
        "execution_admitted": False, "schedule": schedule}
    if panel_ref is not None:
        proof["snapshot_panel"] = panel_ref
    return signals, proof


def _protect_output(out, references):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("A seat availability replay requires a new output directory")
    for reference in references:
        source = Path(reference["file"]).resolve()
        if out == source or out in source.parents or source.parent == out or source.parent in out.parents:
            raise ValueError("Replay output must not cover or sit inside a saved input/source package")
    return out


def _flag(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


def _config(initial_cash: float, holdings: int) -> StrategyConfig:
    return StrategyConfig(
        signal="equal_weight", lookback=21, skip=1, volatility_window=21,
        rebalance_sessions=1, holdings=holdings, retain_rank=max(holdings, 20),
        gross_limit=0.8, single_limit=0.2, initial_cash=initial_cash,
        min_amount=0, min_listing_days=1, commission=0.0003,
        minimum_commission=5, sell_tax=0.0005, transfer_fee=0.00001,
        slippage_bps=10, participation=0.01, dividend_tax_reserve=0.2,
    ).validate()


class SeatPanelFeed:
    def __init__(self, rows, symbols, calendar, signals, lag, availability=None,
                 *, base_hypothesis_id=HYPOTHESIS_ID):
        self.rows = rows
        self.symbols = list(symbols)
        self.calendar = list(calendar)
        self.signals = signals
        self.lag = lag
        self.availability = availability
        self.base_hypothesis_id = base_hypothesis_id
        self.hypothesis_id = availability["method_id"] if availability is not None else base_hypothesis_id
        self.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""}
                            for symbol in self.symbols}
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {"replay_only": True, "scope": "seat_candidate_panel",
                         "hypothesis_id": self.hypothesis_id}
        if availability is not None:
            self.manifest.update(hypothesis_id=self.hypothesis_id, availability_evidence=availability,
                historical_publication_verified=False, selection_enabled=False, execution_admitted=False)
        self.decision_observations = {}
        if availability is not None:
            for day in self.calendar:
                considered = [row for row in availability["schedule"]
                    if row["earliest_lag_signal_day"] == day or row["signal_day"] == day]
                self.decision_observations[day] = {"date": day,
                    "new_signal_symbols": [row["symbol"] for row in signals.get(day, [])],
                    "event_evidence": considered,
                    "reason": ("no_next_session_for_execution" if day == self.calendar[-1] else
                        "known_available_events_only" if signals.get(day) else
                        "seat_availability_unknown_no_new_signal" if any(row["status"] == "unknown" for row in considered) else
                        "seat_availability_late_no_new_signal" if considered else
                        "seat_availability_no_eligible_event")}
        self.volumes = pd.DataFrame(
            {symbol: {day: float(rows[day][symbol]["volume"]) for day in self.calendar}
             for symbol in self.symbols}, index=self.calendar).fillna(0.0)

    def next_day(self, day):
        index = self.calendar.index(day)
        if index + 1 < len(self.calendar):
            return self.calendar[index + 1]
        # The terminal panel session has no observed quote for settlement.  A
        # synthetic later date keeps the Engine's T+1 invariant explicit; no
        # later-session mark or order is inferred from it.
        return (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Seat candidate-panel replay is replay-only")
        # A signal on the terminal observed session has no legal next-session
        # execution frontier. Keep it as an explicit no-decision observation.
        if day == self.calendar[-1]:
            return SignalUnavailable("no_next_session_for_execution")
        candidates = self.signals.get(day)
        if not candidates:
            if self.availability is not None:
                return SignalUnavailable(self.decision_observations[day]["reason"])
            return SignalUnavailable("seat_concentration_no_event")
        return SignalFrame.from_legacy(
            candidates, day,
            f"research:{self.hypothesis_id}:candidate_panel_lag{self.lag}", context,
            available_at=([day + "T15:00:00+08:00"] * len(candidates)
                if self.availability is not None else None),
        )


def load_panel(candidates_path: Path, bars_path: Path, *, lag: int, availability_evidence: Path | None = None):
    if lag not in (1, 2):
        raise ValueError("lag must be 1 or 2")
    candidates = pd.read_csv(candidates_path, dtype={"symbol": str, "trade_date": str})
    required_candidates = {"trade_date", "symbol", "net_buy_to_reported_turnover", "hypothesis_h1"}
    if not required_candidates <= set(candidates.columns):
        raise ValueError("Candidate panel lacks frozen event fields")
    empty_method = None
    if candidates.empty and availability_evidence is not None:
        evidence = json.loads(Path(availability_evidence).read_text(encoding="utf-8-sig"))
        empty_method, _ = _availability_method(evidence)
        reference = _file_ref(candidates_path)
        if (str(Path(evidence["candidates_file"]).resolve()) != reference["file"]
                or evidence.get("candidates_sha256") != reference["sha256"]):
            raise ValueError("Empty candidate batch must bind its exact CSV, not a guessed recipe")
    base_hypothesis_id = _candidate_method(candidates, empty_method=empty_method)
    if base_hypothesis_id == STOCKDAY_HYPOTHESIS_ID and availability_evidence is None:
        raise ValueError("Stock-day v2 requires explicit availability_evidence; legacy lag scheduling is not its method")
    candidates = candidates.loc[candidates.hypothesis_h1.map(_flag)].copy()
    candidates["symbol"] = candidates.symbol.str.upper()
    source_candidates = candidates.copy()
    if availability_evidence is not None:
        # The immutable sidecar describes the complete original candidate CSV,
        # including BSE observations which this Feed cannot execute. Validate
        # their identities/clocks and retain them as explicit outside-scope
        # rows instead of calling their evidence a foreign event.
        source_candidates["score"] = pd.to_numeric(source_candidates.net_buy_to_reported_turnover, errors="coerce")
        if (source_candidates.score.isna().any() or source_candidates.score.lt(.01).any()
                or not source_candidates.score.map(isfinite).all()
                or source_candidates.duplicated(["trade_date", "symbol"]).any()):
            raise ValueError("Availability source candidates have invalid scores or duplicate event identities")
    if base_hypothesis_id == STOCKDAY_HYPOTHESIS_ID:
        for symbol in candidates.symbol:
            stock_board(symbol)  # Reject convertible bonds/BSE even with an SH/SZ suffix.
    candidates = candidates.loc[candidates.symbol.str.endswith((".SSE", ".SZSE"))]
    candidates["score"] = pd.to_numeric(candidates.net_buy_to_reported_turnover, errors="coerce")
    if candidates.score.isna().any() or candidates.score.lt(0.01).any():
        raise ValueError("Candidate panel contains invalid frozen concentration scores")
    if availability_evidence is not None and not candidates.score.map(isfinite).all():
        raise ValueError("Availability replay requires finite frozen event scores")
    if candidates.duplicated(["trade_date", "symbol"]).any():
        raise ValueError("Duplicate candidate event")

    bars = pd.read_csv(bars_path, dtype={"symbol": str, "date": str}, keep_default_na=False)
    required_bars = {"symbol", "date", "available", "open", "high", "low", "close",
                     "volume", "amount", "pre_close", "is_st"}
    if not required_bars <= set(bars.columns):
        raise ValueError("Candidate StockDB bars lack engine fields")
    bars = bars.loc[bars.available.map(_flag) & bars.symbol.str.endswith((".SSE", ".SZSE"))].copy()
    numeric = ["open", "high", "low", "close", "volume", "amount", "pre_close"]
    for column in numeric:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    if bars[numeric].isna().any().any() or bars.duplicated(["symbol", "date"]).any():
        raise ValueError("Candidate bars are incomplete or duplicated")
    dates = sorted(bars.date.unique())
    if len(dates) < lag + 2:
        raise ValueError("Candidate panel needs a prior and at least two replay sessions")
    symbols = sorted(set(candidates.symbol) & set(bars.symbol))
    if candidates.empty and availability_evidence is not None:
        # An observed empty candidate batch is NO_CHANGE. Retain a provided,
        # complete ordinary-stock bar scope so real existing shares can still
        # be valued; do not turn absence of candidates into an empty target.
        symbols = []
        for symbol in sorted(set(bars.symbol)):
            try:
                stock_board(symbol)
            except ValueError:
                continue
            symbols.append(symbol)
    if not symbols:
        raise ValueError("No SH/SZ candidate symbols have StockDB bars")
    path_dates = dates[lag:]
    required_dates = dates[:]
    scope = bars.loc[bars.symbol.isin(symbols) & bars.date.isin(required_dates)]
    if len(scope) != len(symbols) * len(required_dates):
        missing = sorted(set((s, d) for s in symbols for d in required_dates)
                         - set(zip(scope.symbol, scope.date)))
        raise ValueError(f"Candidate panel missing complete OHLC path: {missing[:5]}")

    indexed = scope.set_index(["date", "symbol"])
    rows = {}
    for day in required_dates:
        day_rows = {}
        for symbol in symbols:
            source = indexed.loc[(day, symbol)]
            day_rows[symbol] = {
                "open": float(source.open), "high": float(source.high),
                "low": float(source.low), "close": float(source.close),
                "preclose": float(source.pre_close), "volume": float(source.volume),
                "amount": float(source.amount),
                # StockDB supplies is_st, while tradestatus is an explicit
                # positive-activity proxy for this research-only panel.
                "isST": int(_flag(source.is_st)), "tradestatus": 1,
                "adjustflag": "3",
            }
        rows[day] = day_rows

    if availability_evidence is not None:
        signals, availability = _availability(availability_evidence, candidates_path, bars_path, source_candidates, dates, lag, set(symbols),
            base_hypothesis_id=base_hypothesis_id)
        return SeatPanelFeed(rows, symbols, dates, signals, lag, availability,
            base_hypothesis_id=base_hypothesis_id), candidates, symbols, path_dates
    signals = {}
    date_index = {day: index for index, day in enumerate(dates)}
    for item in candidates.itertuples(index=False):
        event_index = date_index.get(item.trade_date)
        if event_index is None or event_index + lag >= len(dates):
            continue
        signal_day = dates[event_index + lag]
        signals.setdefault(signal_day, []).append({
            "symbol": item.symbol, "score": float(item.score),
            "event_date_ordinal": float(pd.Timestamp(item.trade_date).toordinal()),
        })
    for day in signals:
        signals[day] = sorted(signals[day], key=lambda row: (-row["score"], row["symbol"]))
    return SeatPanelFeed(rows, symbols, dates, signals, lag,
        base_hypothesis_id=base_hypothesis_id), candidates, symbols, path_dates


def replay(candidates_path: Path, bars_path: Path, *, lag: int,
           holdings: int = 5, initial_cash: float = 100000,
           source_audit: Path | None = None, availability_evidence: Path | None = None,
           output_path: Path | None = None) -> dict:
    feed, candidates, symbols, path_dates = load_panel(candidates_path, bars_path, lag=lag,
        availability_evidence=availability_evidence)
    audit_path = Path(source_audit).resolve() if source_audit else Path(bars_path).resolve().parent / "source_bar_pair_audit.json"
    audit_verified = False
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        summary = audit.get("summary") or {}
        audit_verified = (
            audit.get("schema") == "seat_candidate_source_bar_pair_audit_v1"
            and audit.get("status") == "completed_research_only"
            and audit.get("execution_admitted") is False
            and int(summary.get("paired_rows") or 0) >= len(symbols) * len(feed.calendar)
            and all((summary.get("price_stats") or {}).get(field, {}).get("mismatch_rows") == 0
                    for field in ("open", "high", "low", "close", "pre_close"))
            and int((summary.get("amount") or {}).get("mismatch_rows_gt_0_1pct") or 0) == 0
        )
    if not audit_verified:
        raise ValueError(f"Verified candidate source-bar audit required: {audit_path}")
    if feed.availability is not None:
        references = [feed.availability[key] for key in ("evidence_file", "candidates_file", "bars_file")]
        references += feed.availability["source_files"] + [_file_ref(audit_path)]
        if "snapshot_panel" in feed.availability:
            references.append(feed.availability["snapshot_panel"])
        if any(Path(row["file"]) == audit_path for row in feed.availability["source_files"]):
            raise ValueError("Disclosure evidence cannot use the derived source-bar audit as its original source")
        feed.availability["source_bar_pair_audit"] = _file_ref(audit_path)
        if output_path is not None:
            _protect_output(output_path, references)
    config = _config(initial_cash, holdings)
    context = RunContext(f"seat-panel-replay:{feed.hypothesis_id}:lag{lag}", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    daily = []
    for index, day in enumerate(path_dates):
        daily.append(advance(state, feed, day, config, index, context=context))
    result = {
        "schema": "seat_hypothesis_candidate_panel_replay_v1",
        "status": "completed_research_only_candidate_panel_replay",
        "hypothesis_id": feed.hypothesis_id,
        "lag": lag,
        "candidate_panel_only": True,
        "symbols": len(symbols), "event_rows": int(len(candidates)),
        "replay_sessions": len(path_dates), "start_date": path_dates[0],
        "end_date": path_dates[-1], "signal_days": sorted(feed.signals),
        "config": config.to_dict(),
        "metrics": metrics(daily, config.initial_cash),
        "daily": daily,
        "orders": list(state["orders"].values()),
        "fills": list(state["fills"].values()),
        "state": state,
        "engine_ledger_replay_completed": True,
        "target_holdings_generated": True,
        "paper_orders_generated": bool(state["orders"]),
        "ledger_mutated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "selection_enabled": False,
        "strategy_validated": False,
        "future_labels_used_for_score": False,
        "source_bar_pair_audit": str(audit_path),
        "source_bar_pair_audit_verified": audit_verified,
        "limitations": [
            "This is a candidate-panel replay, not a full-market or untouched strategy validation.",
            "The panel uses a positive-activity tradestatus proxy; PIT membership and company actions are unresolved.",
            "StockDB raw OHLC and a synthetic 100000 RMB account are research inputs, not broker observations.",
            "Missing BSE symbols are excluded explicitly; no symbol or label is imputed.",
        ],
    }
    if feed.availability is not None:
        result.update(method_id=feed.hypothesis_id, base_hypothesis_id=feed.base_hypothesis_id,
            availability_evidence=feed.availability,
            decision_observations=[feed.decision_observations[day] for day in path_dates],
            historical_publication_verified=False,
            target_holdings_generated=bool(feed.signals))
        if not result["fills"]:
            result["metrics"] = None
            result["performance_unavailable_reason"] = "no_actual_simulated_fills_not_a_zero_return"
        result["limitations"] += [
            "Saved byte hashes and declared clocks are checked; historical publisher availability is not independently verified.",
            "Unknown events are retained without signals; known events keep the original score order, not a complete disclosure batch.",
            "Original evidence must be available by the observed session 15:00; the derived SignalFrame clock is distinct from source publication.",
            "The observed bar calendar is the existing small panel, not an independently verified complete exchange calendar.",
            "No-event decisions preserve holdings; there is no forced terminal liquidation, so surviving positions are right-censored.",
        ]
    if feed.base_hypothesis_id == STOCKDAY_HYPOTHESIS_ID:
        result["method_id"] = feed.hypothesis_id
        result["base_hypothesis_id"] = STOCKDAY_HYPOTHESIS_ID
        result["limitations"] += [
            "Stock-day v2 sums each eligible canonical net once and uses one concordant single-day turnover, not the legacy repeated sum.",
            "Ordinary SH/SZ scope uses the existing stock_board prefix rule, not verified historical membership or personal seat identity.",
        ]
    return result


def write_result(result: dict, out: Path):
    out = Path(out).resolve()
    if "availability_evidence" in result:
        proof = result["availability_evidence"]
        _protect_output(out, [proof[key] for key in ("evidence_file", "candidates_file", "bars_file", "source_bar_pair_audit")]
            + proof["source_files"] + ([proof["snapshot_panel"]] if "snapshot_panel" in proof else []))
    out.mkdir(parents=True, exist_ok=False)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(result["daily"]).to_csv(out / "daily.csv", index=False)
    pd.DataFrame(result["orders"]).to_csv(out / "orders.csv", index=False)
    pd.DataFrame(result["fills"]).to_csv(out / "fills.csv", index=False)
    (out / "account.json").write_text(json.dumps(result["state"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    performance = (f"Net return: `{result['metrics']['net_return']:.4%}`; max drawdown: `{result['metrics']['max_drawdown']:.4%}`."
        if result["metrics"] is not None else "Performance unavailable: no simulated fills; cash-only observations are not a zero-return strategy result.")
    report = [
        "# Public-seat hypothesis candidate-panel replay", "",
        f"Lag: `{result['lag']}`; symbols: `{result['symbols']}`; sessions: `{result['replay_sessions']}`.",
        f"Orders/fills: `{len(result['orders'])}/{len(result['fills'])}`; execution admitted: `false`.",
        performance,
        "This is a candidate-panel Engine/Ledger rehearsal, not a complete-market strategy result.",
        "", *["- " + item for item in result["limitations"]],
    ]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return out / "result.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--lag", required=True, type=int, choices=(1, 2))
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--source-audit", type=Path)
    parser.add_argument("--availability-evidence", type=Path,
        help="Explicit new method: verify original disclosure bytes and actual availability clocks")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = replay(args.candidates, args.bars, lag=args.lag, holdings=args.holdings,
                    initial_cash=args.initial_cash, source_audit=args.source_audit,
                    availability_evidence=args.availability_evidence, output_path=args.out)
    path = write_result(result, args.out)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "orders": len(result["orders"]), "fills": len(result["fills"]),
                      "net_return": result["metrics"]["net_return"] if result["metrics"] is not None else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
