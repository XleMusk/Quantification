"""Consume the unchanged completed low-volatility rank cache.

No feature or ranking is recomputed.  The cohort comes from the original
snapshot code columns, never the union of subsequently selected stocks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from ..rules import stock_board
from ..symbols import normalize_symbol
from .full_market import verify_chunks
from .monthly_timing import month_ends
from .stockdb_rolling_low_volatility import portfolio


VARIANT = "low_volatility_252_20"
METHOD_ID = "saved_lowvol_rank30_original_full_scope_signals_v1"
MULTIYEAR_INCEPTION_CONTEXT = "fixed_multiyear_calendar_predecessor_new_cash_v1"


@dataclass(frozen=True)
class SavedLowvolSignals:
    config: StrategyConfig
    symbols: list[str]
    signals: dict[str, list[dict]]
    decisions: dict[str, str]
    preceding_session: str
    provenance: dict


def _json(path):
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"Saved low-volatility object required: {path}")
    return result


def _dates(values):
    days = list(values)
    if not days or days != sorted(set(days)) or any(not isinstance(day, str)
            or date.fromisoformat(day).isoformat() != day for day in days):
        raise ValueError("Exact chronological unique sessions required")
    return days


def load_saved_lowvol_signals(run_path, start, end, fixed_sessions, *, inception_context=None):
    fixed = _dates(fixed_sessions)
    _dates([start] if start == end else [start, end])
    if inception_context is not None and (inception_context != MULTIYEAR_INCEPTION_CONTEXT or start[:4] == end[:4]):
        raise ValueError("Only the explicit multi-year new-cash inception context is supported")
    if start not in fixed or end not in fixed or not any(day > end for day in fixed):
        raise ValueError("Exact start/end and a calendar successor are required")
    requested = [day for day in fixed if start <= day <= end]
    run_path = Path(run_path).resolve()
    if run_path.is_dir():
        run_path /= "run.json"
    run = _json(run_path)
    if (run.get("schema") != "stockdb_rolling_low_volatility_ledger_v1"
            or run.get("status") != "completed_research_only" or run.get("research_only") is not True
            or run.get("execution_admitted") is not False or run.get("variants") != [VARIANT]
            or not run["replay_start"] <= start <= end <= run["replay_end"]):
        raise ValueError("A completed original single-variant low-volatility run covering the request is required")
    config = StrategyConfig(**run["config"]).validate()
    if config != portfolio(config.initial_cash):
        raise ValueError("The original fixed low-volatility portfolio config changed")
    artifacts = Path(run["artifacts"]).resolve()
    protocol_path = artifacts / "protocol.json"
    protocol = _json(protocol_path)
    if (protocol.get("schema") != run["schema"] or protocol.get("variants") != [VARIANT]
            or protocol.get("config") != run["config"] or protocol.get("lookback") != 252
            or protocol.get("skip") != 21 or protocol.get("rank_cadence") != "monthly_completed_session_end"
            or any(protocol.get(key) != run.get(key) for key in
                   ("replay_start", "replay_end", "symbols", "sessions", "supplemental_raw_view"))):
        raise ValueError("Saved low-volatility protocol does not match its original run")
    snapshot = Path(protocol["snapshot"]).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = _json(manifest_path)
    if protocol.get("snapshot_manifest") != manifest:
        raise ValueError("Original low-volatility snapshot manifest changed")
    verify_chunks(snapshot, manifest)
    symbols = set()
    for item in manifest["chunks"]:
        for code in pd.read_parquet(snapshot / item["parquet_file"], columns=["code"])["code"].unique():
            symbol = normalize_symbol(str(code).zfill(6)).vt_symbol
            try:
                stock_board(symbol)
            except ValueError:
                continue
            symbols.add(symbol)
    symbols = sorted(symbols)
    if len(symbols) != run["symbols"]:
        raise ValueError("Original supported full-snapshot cohort count differs")
    rank_path = artifacts / f"{VARIANT}_ranks.csv"
    decision_path = artifacts / f"{VARIANT}_decisions.csv"
    ranks = pd.read_csv(rank_path, dtype={"date": str, "variant": str, "symbol": str})
    decisions = pd.read_csv(decision_path, dtype={"date": str, "variant": str, "decision": str})
    if (not {"date", "variant", "rank", "symbol", "volatility", "momentum", "median_amount"}.issubset(ranks)
            or not {"date", "variant", "decision", "eligible_count", "candidate_count"}.issubset(decisions)
            or not decisions.variant.eq(VARIANT).all() or not ranks.variant.eq(VARIANT).all()
            or decisions.date.duplicated().any() or ranks.duplicated(["date", "symbol"]).any()):
        raise ValueError("Original rank and decision cache schema, variant or uniqueness differs")
    observed = _dates(sorted(decisions.date.tolist()))
    if [day for day in observed if start <= day <= end] != requested:
        raise ValueError("Exact fixed-calendar decision coverage is missing or contains non-session dates")
    previous = [day for day in observed if day < start]
    preceding_basis = "exact_previous_original_observed_decision_day"
    if not previous and inception_context == MULTIYEAR_INCEPTION_CONTEXT:
        if start != run["replay_start"] or start != observed[0] or fixed.index(start) == 0:
            raise ValueError("New-cash inception context requires the original first decision and exact calendar predecessor")
        preceding = fixed[fixed.index(start) - 1]
        preceding_basis = "explicit_multiyear_fixed_calendar_predecessor_context_only"
    elif not previous:
        raise ValueError("The exact preceding original observed decision session is unavailable")
    else:
        preceding = previous[-1]
        if inception_context is not None and (fixed.index(start) == 0 or preceding != fixed[fixed.index(start) - 1]):
            raise ValueError("Saved preceding decision is not the exact multi-year calendar predecessor")
    monthly = set(month_ends(fixed, end))
    selected = decisions.loc[decisions.date.isin(requested)].set_index("date")
    signals, reasons, unavailable = {}, {}, {}
    allowed = {"monthly_ranking", "not_month_end", "252_session_warmup"}
    for day in requested:
        decision = selected.loc[day]
        reason = decision["decision"]
        if reason not in allowed:
            raise ValueError(f"Unknown saved decision cannot become an empty target: {day} {reason}")
        rows = ranks.loc[ranks.date.eq(day)].copy()
        if (reason in {"monthly_ranking", "252_session_warmup"}) != (day in monthly):
            raise ValueError(f"Saved decision disagrees with exact fixed month-end schedule: {day}")
        reasons[day] = reason
        if reason != "monthly_ranking":
            if not rows.empty:
                raise ValueError(f"Non-ranking decision has rank rows: {day}")
            unavailable[day] = reason
            continue
        counts = pd.to_numeric(decision[["eligible_count", "candidate_count"]], errors="raise")
        if (not np.isfinite(counts).all() or (counts < 0).any()
                or not counts.eq(counts.astype(int)).all() or counts.eligible_count != counts.candidate_count):
            raise ValueError(f"Invalid original monthly candidate counts: {day}")
        expected = min(config.retain_rank, int(counts.eligible_count))
        rows["rank"] = pd.to_numeric(rows["rank"], errors="raise")
        rows = rows.sort_values("rank", kind="stable")
        if (len(rows) != expected or rows["rank"].tolist() != list(range(1, expected + 1))
                or not rows.symbol.isin(symbols).all()):
            raise ValueError(f"Incomplete or non-original rank30 cache: {day}")
        for field in ("volatility", "momentum", "median_amount"):
            rows[field] = pd.to_numeric(rows[field], errors="raise")
            if not np.isfinite(rows[field]).all():
                raise ValueError(f"Nonfinite original rank feature: {day} {field}")
        if rows.volatility.le(0).any() or rows.median_amount.lt(config.min_amount).any():
            raise ValueError(f"Original saved ranking violates its fixed eligibility: {day}")
        if expected == 0:
            unavailable[day] = "known_empty_eligible_pool"
            continue
        signals[day] = [{"symbol": row.symbol, "score": -float(row.volatility),
            "volatility": float(row.volatility), "momentum": float(row.momentum)}
            for row in rows.itertuples()]
    captured = datetime.now(MARKET_TZ).isoformat()
    pins = [{"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in (run_path, protocol_path, rank_path, decision_path, manifest_path)]
    provenance = {"schema": "saved_lowvol_signal_cache_v1", "method_id": METHOD_ID,
        "run": str(run_path), "artifacts": str(artifacts), "snapshot": str(snapshot),
        "source_symbols": len(symbols), "original_replay_start": run["replay_start"],
        "original_replay_end": run["replay_end"], "requested_start": start, "requested_end": end,
        "preceding_session": preceding, "preceding_session_basis": preceding_basis,
        "preceding_year_official_calendar_independently_verified": False,
        "signal_days": sorted(signals), "unavailable_signal_reasons": unavailable,
        "pins_captured_at": captured, "pins": pins,
        "pin_clock_basis": "current_read_hash_capture_not_original_historical_seal_or_publication",
        "fixed_signal_rule": "original_saved_rank30_negative_volatility_no_reranking",
        "scope_basis": "all_original_snapshot_supported_codes_not_future_rank_union",
        "initial_account_basis": "new_cash_account_no_implicit_prior_year_positions_or_pending_signal",
        "initial_prior_year_signal_consumed": False, "feature_recomputed": False,
        "ranks_recomputed": False, "snapshot_chunk_hashes_verified": True,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
        "candidate_panel_only": True, "model_refit": False, "future_labels_used_for_score": False}
    if inception_context is not None:
        provenance.update(inception_context_method=inception_context,
            preceding_context_signal_consumed=False, prior_pending_signal_invented=False)
    return SavedLowvolSignals(config, symbols, signals, reasons, preceding, provenance)
