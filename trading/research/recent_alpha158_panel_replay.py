"""Fixed two-case, late-derived recent Alpha158 Engine/Ledger diagnostic.

Ranking never consults successor prices. Missing execution/reference evidence
keeps the selected members and yields NO_CHANGE, never a replacement stock.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from collections.abc import Mapping

import numpy as np
import pandas as pd

from ..contracts import RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from ..report import metrics
from .baostock_daily_slice import digest
from .ipo_qualification import IpoQualifiedResearchFeed, load_ipo_qualification
from .leader_daily_replay import CommonCohortDecisionFeed
from .leader_daily_signals import _calendar_view
from .recent_source_alpha158 import METHOD_ID as SCORE_METHOD, _active_scope, _aware
from .recent_source_qlib_provider import METHOD_ID as PROVIDER_METHOD, SCHEMA as PROVIDER_SCHEMA
from .shortline_decision_v2 import BATCH_SCHEMA
from .shortline_panel_replay import ShortlinePanelFeed
from .stockdb_alpha158_lgbm_panel_replay import _config, _first_seen_cap


SCHEMA = "a_share_recent_source_alpha158_candidate_panel_replay_v1"
PROTOCOL = {"id": "recent_alpha158_fixed_two_candidate_panels_v1", "top_per_day": 5, "holdings": 5,
    "initial_cash": 100000, "cases": [{"id": "causal_first_seen_cap20", "max_symbols": 20},
                                      {"id": "uncapped", "max_symbols": None}],
    "ranking": "each_day_rank_mean_desc_symbol_asc_before_successor_or_raw_checks_no_refill",
    "reference_bound": .01, "reference_unknown": "NO_CHANGE_not_assume_preclose_equals_prior_close",
    "next_raw_check": "execution_data_quality_witness_not_alpha_selection_or_historical_publication",
    "held_reference_unknown_or_incompatible": "stop_before_session_no_complete_PnL",
    "corporate_action_coverage_verified": False, "parameters_selected_by_PnL": False}
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "ledger_mutated": False, "persistent_ledger_mutated": False, "provider_contacted": False,
    "account_created": False, "ths_payload_generated": False, "company_actions_verified": False,
    "pit_verified": False, "historical_signal_publication_verified": False}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load(batch_run, calendar):
    path = Path(batch_run).resolve()
    path = path / "result.json" if path.is_dir() else path
    score = _json(path)
    now = datetime.now().astimezone()
    if (score.get("schema") != "a_share_recent_source_alpha158_frozen_score_batch_v1"
            or score.get("status") != "completed_research_only_dated_score_batch" or score.get("method_id") != SCORE_METHOD
            or any(score.get(key) is not False for key in ("model_refit", "model_trained", "future_labels_used_for_score",
                "strategy_validated", "selection_enabled", "execution_admitted", "ledger_mutated",
                "target_holdings_generated", "paper_orders_generated", "ths_payload_generated"))
            or _aware(score["actual_computed_at"]) > now):
        raise ValueError("Completed unadmitted frozen recent-source score batch required")
    folder = Path(score["provider_view"]).resolve()
    provider = _json(folder / "result.json")
    calendar = Path(calendar).resolve()
    if (digest(folder / "result.json") != score["provider_result_sha256"]
            or provider.get("schema") != PROVIDER_SCHEMA or provider.get("provider_method_id") != PROVIDER_METHOD
            or provider.get("status") != "research_provider_completed"
            or provider.get("raw_prices_imputed") is not False
            or provider.get("recent_source_inventory_complete") is not True
            or provider.get("recent_raw_original_fields_unchanged") is not True
            or any(provider.get(key) is not False for key in ("model_refit", "model_trained", "execution_admitted", "selection_enabled",
                "strategy_validated", "future_labels_used_for_score"))
            or digest(calendar) != provider["calendar_sha256"]
            or score.get("source_proofs") != provider["source_proofs"]
            or not _aware(provider["created_at"]) <= _aware(score["actual_computed_at"]) <= now):
        raise ValueError("Score/provider/calendar lineage or actual clocks changed")
    payload = _json(calendar)
    year = int(payload["year"])
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    days = provider["recent_source_days"]
    if (status != "pinned_static_exchange_calendar" or len(days) < 3 or days != sorted(set(days))
            or [day for day in sessions if days[0] <= day <= days[-1]] != days
            or [proof["day"] for proof in provider["source_proofs"]] != days
            or any(proof.get("read_only_verified") is not True or proof.get("source_inventory_complete") is not True
                or proof.get("full_dated_roster_inventory_complete") is not True
                or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"
                or proof.get("calendar_sha256") != digest(calendar) for proof in provider["source_proofs"])):
        raise ValueError("Own full recent source dates must be consecutive pinned sessions")
    raw_path = Path(provider["recent_raw_observations_file"]).resolve()
    raw_path.relative_to(folder)
    if (digest(raw_path) != provider["recent_raw_observations_sha256"]
            or score.get("raw_observations_sha256") != provider["recent_raw_observations_sha256"]
            or Path(score["raw_observations"]).resolve() != raw_path):
        raise ValueError("Exact saved raw observation proof changed")
    raw = pd.read_parquet(raw_path)
    required = {"code", "date", "open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus", "isST", "pctChg"}
    if (not required <= set(raw) or len(raw) != provider["recent_raw_observation_rows"]
            or raw.duplicated(["date", "code"]).any()
            or raw.groupby("date").size().to_dict() != {p["day"]: p["ordinary_inventory_rows"] for p in provider["source_proofs"]}):
        raise ValueError("All own-dated ordinary raw rows must remain present")
    scores_path = Path(score["score_path"]).resolve()
    scores_path.relative_to(path.parent)
    scores = pd.read_parquet(scores_path)
    fields = ["rank_mean", "score_seed17", "score_seed29", "score_seed43"]
    if not {"date", "symbol", *fields} <= set(scores):
        raise ValueError("Dated frozen seed scores missing")
    scores["date"] = pd.to_datetime(scores.date, errors="raise").dt.strftime("%Y-%m-%d")
    scores[fields] = scores[fields].apply(pd.to_numeric, errors="raise")
    requested = score["score_dates"]
    active, _ = _active_scope(raw, requested)
    observed = set(zip(scores.date, scores.symbol))
    expected = {(day.strftime("%Y-%m-%d"), symbol) for day, symbol in active}
    if (scores.duplicated(["date", "symbol"]).any() or not np.isfinite(scores[fields]).all().all()
            or len(scores) != score["score_rows"] or observed != expected
            or sorted(scores.date.unique()) != requested or not set(requested) <= set(days)):
        raise ValueError("Scores must retain exact complete active current-date scope, not successor survivors")
    selected = scores.sort_values(["date", "rank_mean", "symbol"], ascending=[True, False, True], kind="stable").groupby("date", sort=True).head(5)
    selected = selected[["date", "symbol", *fields]].rename(columns={"rank_mean": "score"}).reset_index(drop=True)
    proof = {"batch_run": str(path), "batch_sha256": digest(path), "score_path": str(scores_path),
        "score_sha256": digest(scores_path), "score_dates": requested, "score_rows": len(scores),
        "provider_view": str(folder), "provider_result_sha256": score["provider_result_sha256"],
        "raw_observations_sha256": provider["recent_raw_observations_sha256"], "source_proofs": provider["source_proofs"],
        "score_computed_at": score["actual_computed_at"], "source_available_at": provider["created_at"]}
    anchor, anchor_proof = _context_anchor(provider)
    proof["context_anchor"] = anchor_proof
    return selected, raw, days, sessions, previous, proof, anchor


def _context_anchor(provider):
    """Read only exact pre-prefix closes from the original pinned partitions."""
    snapshot = Path(provider["history_snapshot"]).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = _json(manifest_path)
    day = provider["base_history_end"]
    day_int = int(day.replace("-", ""))
    if (digest(manifest_path) != provider["original_history_manifest_sha256"]
            or manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False
            or len(manifest.get("chunks", [])) != manifest.get("chunks_expected")):
        raise ValueError("Context anchor original snapshot manifest changed")
    parts = [item for item in manifest["chunks"] if str(item["prefix"]) in {"0", "3", "6"}
        and int(item["lower_inclusive"]) <= day_int <= int(item["upper_inclusive"])]
    if sorted(str(item["prefix"]) for item in parts) != ["0", "3", "6"]:
        raise ValueError("One exact context-anchor partition per original query prefix required")
    pieces, refs = [], []
    for item in parts:
        path = (snapshot / item["parquet_file"]).resolve()
        path.relative_to(snapshot)
        if digest(path) != item["parquet_sha256"]:
            raise ValueError("Context anchor partition hash changed")
        frame = pd.read_parquet(path, columns=["code", "date", "close"])
        if len(frame) != int(item["rows"]):
            raise ValueError("Context anchor partition row count changed")
        frame = frame.loc[pd.to_numeric(frame.date, errors="raise").eq(day_int)].copy()
        if (not frame.code.astype(str).str.fullmatch(r"\d{6}").all()
                or not frame.code.astype(str).str.startswith(str(item["prefix"])).all()):
            raise ValueError("Context anchor code identity differs from its query prefix")
        pieces.append(frame)
        refs.append({"file": str(path), "sha256": item["parquet_sha256"], "prefix": str(item["prefix"]),
                     "partition_rows": item["rows"], "exact_anchor_rows": len(frame)})
    frame = pd.concat(pieces, ignore_index=True)
    if frame.code.duplicated().any():
        raise ValueError("Duplicate exact context anchor code")
    prices = pd.to_numeric(frame.close, errors="coerce")
    valid = np.isfinite(prices) & prices.gt(0)
    anchor = {str(code) + (".SSE" if str(code).startswith("6") else ".SZSE"): float(price)
        for code, price in zip(frame.loc[valid, "code"], prices.loc[valid])}
    return {"day": day, "closes": anchor}, {"day": day, "history_snapshot": str(snapshot),
        "original_history_manifest_sha256": provider["original_history_manifest_sha256"], "partitions": refs,
        "valid_anchor_codes": len(anchor), "invalid_anchor_rows": int((~valid).sum()),
        "only_reference_context_not_quote_or_trade_session": True, "PIT_or_actions_verified": False}


class _BoundedRows(Mapping):
    """Compact day-indexed raw rows with one materialised day at a time."""
    def __init__(self, frame, days, symbols):
        self.symbols = tuple(symbols)
        self._symbol_index = {symbol: index for index, symbol in enumerate(self.symbols)}
        fields = ["open", "high", "low", "close", "preclose", "volume", "amount"]
        self._days = tuple(days)
        self._payload = {}
        volume_grid = np.full((len(self._days), len(self.symbols)), np.nan, dtype=float)
        day_index = {day: index for index, day in enumerate(self._days)}
        for day, group in frame.groupby("date", sort=False):
            if day not in day_index:
                continue
            codes = group["symbol"].to_numpy()
            indexes = np.fromiter((self._symbol_index.get(symbol, -1) for symbol in codes), dtype=np.int32,
                                  count=len(codes))
            valid = indexes >= 0
            indexes, group = indexes[valid], group.iloc[np.flatnonzero(valid)]
            values = {field: pd.to_numeric(group[field], errors="coerce").to_numpy(dtype=float, copy=True)
                      for field in fields}
            statuses = group["tradestatus"].map({"0": 0, "1": 1}).to_numpy()
            st_flags = group["isST"].map({"0": 0, "1": 1}).to_numpy()
            self._payload[day] = (indexes, values, statuses, st_flags)
            volume_grid[day_index[day], indexes] = values["volume"]
        self.volumes = pd.DataFrame(volume_grid, index=self._days, columns=self.symbols)
        self._cached_day = None
        self._cached_rows = None

    def __getitem__(self, day):
        if day not in self._payload:
            raise KeyError(day)
        if day == self._cached_day:
            return self._cached_rows
        indexes, values, statuses, st_flags = self._payload[day]
        rows = {}
        for offset, symbol_index in enumerate(indexes):
            status = statuses[offset]
            st_flag = st_flags[offset]
            rows[self.symbols[symbol_index]] = {
                **{field: float(values[field][offset]) for field in values},
                "tradestatus": int(status) if status in (0, 1) else None,
                "isST": int(st_flag) if st_flag in (0, 1) else None,
                "adjustflag": "3",
            }
        self._cached_day, self._cached_rows = day, rows
        return rows

    def __iter__(self):
        return iter(self._payload)

    def __len__(self):
        return len(self._payload)


class ExactSourceRawFeed(ShortlinePanelFeed):
    """Narrow Engine surface retaining raw status and nullable activity."""
    def __init__(self, raw, days, symbols, context_anchor=None, *, copy_raw=True, bounded_memory=False):
        self.symbols, self.calendar, self.signals = list(symbols), list(days), {}
        self.context_anchor = context_anchor or {"day": None, "closes": {}}
        required = ["code", "date", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "tradestatus", "isST"]
        missing = set(required) - set(raw)
        if missing:
            raise ValueError(f"Exact source raw is missing required fields: {sorted(missing)}")
        frame = raw.loc[:, required].copy() if copy_raw else raw
        frame["symbol"] = frame.code.str[3:] + np.where(frame.code.str.startswith("sh."), ".SSE", ".SZSE")
        frame = frame.loc[frame.symbol.isin(symbols)]
        fields = ["open", "high", "low", "close", "preclose", "volume", "amount"]
        frame[fields] = frame[fields].apply(pd.to_numeric, errors="coerce")
        if bounded_memory:
            bounded = _BoundedRows(frame, days, symbols)
            self.rows, self.volumes = bounded, bounded.volumes
        else:
            self.rows = {}
            for day, group in frame.groupby("date", sort=False):
                values = {}
                for row in group.to_dict("records"):
                    values[row["symbol"]] = {**{field: float(row[field]) for field in fields},
                        "tradestatus": int(row["tradestatus"]) if row["tradestatus"] in ("0", "1") else None,
                        "isST": int(row["isST"]) if row["isST"] in ("0", "1") else None, "adjustflag": "3"}
                self.rows[day] = values
            self.volumes = frame.pivot(index="date", columns="symbol", values="volume").reindex(index=days, columns=symbols)
        self.instruments = {symbol: {"ipoDate": None, "outDate": ""} for symbol in symbols}
        self.actions, self.instrument_profiles = [], {}
        self.manifest = {"replay_only": True, "scope": "exact_saved_recent_ordinary_raw_candidate_panel",
            "listing_date_basis": "unknown_until_explicit_IPO_overlay_never_first_observed",
            "corporate_actions_verified": False, "execution_admitted": False}


class _QualifiedDecisionFeed(IpoQualifiedResearchFeed):
    def signal_frame(self, day, config, context):
        return self.base.signal_frame(day, config, context)


def _reference(base, day, symbol, previous):
    prior = previous.get(day)
    row, before = base.rows.get(day, {}).get(symbol), base.rows.get(prior, {}).get(symbol)
    if before is None and prior == base.context_anchor["day"] and symbol in base.context_anchor["closes"]:
        before = {"close": base.context_anchor["closes"][symbol]}
    if not row or not before or not np.isfinite(row["preclose"]) or not np.isfinite(before["close"]) or before["close"] <= 0:
        return {"status": "unknown", "day": day, "previous_day_required": prior, "symbol": symbol,
                "reason": "exact_previous_raw_reference_unavailable"}
    ratio = row["preclose"] / before["close"] - 1
    return {"status": "consistent_proxy" if abs(ratio) <= .01 else "incompatible", "day": day,
        "previous_day_required": prior, "symbol": symbol, "preclose_to_previous_close_ratio_minus1": float(ratio),
        "absence_of_corporate_actions_proven": False}


def _raw_issues(base, day, members):
    issues = []
    for symbol in sorted(members):
        row = base.rows.get(day, {}).get(symbol)
        if row is None:
            issues.append({"day": day, "symbol": symbol, "reason": "raw_row_missing"})
        elif (row["tradestatus"] not in (0, 1) or row["isST"] not in (0, 1)
                or any(not np.isfinite(row[field]) for field in ("open", "high", "low", "close", "preclose", "volume", "amount"))
                or any(row[field] <= 0 for field in ("open", "high", "low", "close", "preclose"))
                or row["volume"] < 0 or row["amount"] < 0):
            issues.append({"day": day, "symbol": symbol, "reason": "required_raw_status_or_values_unknown"})
    return issues


def _case(selected, raw, days, previous, qualification, case, *, context_anchor=None):
    candidates, dropped = (selected.copy(), 0) if case["max_symbols"] is None else _first_seen_cap(selected, 20)
    symbols = sorted(candidates.symbol.unique())
    base = ExactSourceRawFeed(raw, days, symbols, context_anchor=context_anchor)
    config, state = _config(100000, 5), new_state(100000)
    batches = {day: {"schema": BATCH_SCHEMA, "day": day, "status": "incomplete", "candidates": [],
        "source": "research:recent_source_alpha158_fixed_top5"} for day in days}
    grouped = {day: group[["symbol", "score"]].to_dict("records") for day, group in candidates.groupby("date", sort=True)}
    common = CommonCohortDecisionFeed(base, batches, state, market_defense=False)
    feed = _QualifiedDecisionFeed(common, qualification)
    context = RunContext("recent-source-alpha158:" + case["id"], TradingMode.REPLAY)
    daily, decisions, blocked = [], [], None
    for index, day in enumerate(days):
        events = grouped.get(day, [])
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        held_refs = [_reference(base, day, symbol, previous) for symbol in sorted(held)]
        held_unknown = _raw_issues(base, day, held)
        if held_unknown or any(item["status"] != "consistent_proxy" for item in held_refs):
            blocked = {"day": day, "reason": "held_raw_or_action_reference_unresolved", "raw_issues": held_unknown,
                       "reference_evidence": held_refs, "session_not_committed": True}
            break
        required = held | set(state.get("pending_targets") or {}) | {event["symbol"] for event in events}
        following = days[index + 1] if index + 1 < len(days) else None
        reasons, raw_issues, refs, ipo = [], [], [], []
        if not events:
            reasons.append("score_batch_missing")
        if following is None:
            reasons.append("withheld_missing_exact_successor_full_source")
        else:
            raw_issues = _raw_issues(base, day, required) + _raw_issues(base, following, required)
            refs = [_reference(base, date, symbol, previous) for date in (day, following) for symbol in sorted(required)]
            ipo = [qualification.assess(symbol, date, decision_as_of=date + ("T15:05:00+08:00" if date == day else "T09:30:00+08:00"))
                for date in (day, following) for symbol in sorted(required)]
            if raw_issues:
                reasons.append("required_current_or_successor_raw_unknown")
            if any(item["status"] != "consistent_proxy" for item in refs):
                reasons.append("current_or_execution_reference_unknown_or_incompatible")
            if any(item["engine_block_reason"] is not None for item in ipo):
                reasons.append("required_IPO_qualification_unknown_or_unsupported_at_own_clock")
        batch = common.batches[day]
        batch.update(status="complete" if not reasons else "incomplete", candidates=events)
        trial = deepcopy(state)
        common.state = trial
        try:
            row = advance(trial, feed, day, config, index, context=context)
        except (ValueError, KeyError) as error:
            blocked = {"day": day, "reason": "shared_engine_session_blocked", "error": f"{type(error).__name__}: {error}", "session_not_committed": True}
            break
        state = trial
        daily.append(row)
        no_change = next((event for event in row["events"] if event["kind"] == "no_decision"), None)
        decisions.append({"day": day, "execution_day": following, "status": "NO_CHANGE" if no_change else "FULL_TARGET",
            "candidates": events, "candidate_count": len(events), "required_members": sorted(required), "reasons": reasons,
            "raw_issues": raw_issues, "reference_evidence": refs, "IPO_evidence": ipo,
            "no_change_reason": no_change["reason"] if no_change else None,
            "decision_cutoff": day + "T15:05:00+08:00", "successor_IPO_knowledge_cutoff": following + "T09:30:00+08:00" if following else None,
            "successor_raw_witness_is_post_close": True,
            "successor_prices_used_for_ranking_or_refill": False, "successor_gate_is_retrospective_quality_witness": True})
    return {"case": case["id"], "max_symbols": case["max_symbols"], "causal_cap_dropped_rows": dropped,
        "status": "blocked_diagnostic_no_complete_PnL" if blocked else "completed_conditional_raw_account_diagnostic",
        "selected_events": candidates.to_dict("records"), "future_path_dropped_rows": 0,
        "config": config.to_dict(), "decisions": decisions, "daily": daily,
        "orders": list(state["orders"].values()), "fills": list(state["fills"].values()), "account": state,
        "blocking_evidence": blocked, "metrics": metrics(daily, 100000) if daily and blocked is None else None,
        "metrics_semantics": "conditional_unadjusted_raw_engine_cash_account_not_verified_economic_total_return",
        "engine_ledger_replay_completed": blocked is None, **FLAGS}


def run(batch_run, calendar, ipo_evidence, research_master_run, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("A recent score replay requires a new output directory")
    candidates, raw, days, sessions, previous, source, anchor = _load(batch_run, calendar)
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    if tuple(qualification.calendar) != tuple(sessions):
        raise ValueError("Replay and explicit IPO qualification calendars differ")
    cases = [_case(candidates, raw, days, previous, qualification, case, context_anchor=anchor) for case in PROTOCOL["cases"]]
    result = {"schema": SCHEMA, "status": "completed_research_only_candidate_panel_diagnostics",
        "created_at": datetime.now().astimezone().isoformat(), "protocol": PROTOCOL,
        "source": source, "calendar": str(Path(calendar).resolve()), "calendar_sha256": digest(calendar),
        "ipo_qualification": qualification.verification, "cases": cases,
        "case_selection_performed": False, "target_holdings_generated": any(case["account"]["targets"] for case in cases),
        "paper_orders_generated": any(case["orders"] for case in cases), "persistent_account_used": False, **FLAGS,
        "limitations": ["Late-derived dated frozen ranks, not historical signal publication or untouched validation",
            "Pre-prefix raw close is separately hash/row-bound reference context only; missing members retain NO_CHANGE and are never price-filled",
            "Exact successor data only gates raw/qualification quality; it never ranks, removes or replaces selected candidates",
            "Corporate actions remain incomplete even when the one-percent raw reference proxy is consistent",
            "Blocked held-price/action sessions stop before committing trial memory state and have no complete-PnL claim",
            "Both fixed candidate panels are retained; no account, broker, THS, network or persistent Ledger is used"]}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    for case in cases:
        folder = out / case["case"]
        folder.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(folder / (key + ".json"), case[key])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-run", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--ipo-evidence", required=True, type=Path)
    parser.add_argument("--research-master-run", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run(args.batch_run, args.calendar, args.ipo_evidence, args.research_master_run, args.out)
    print(json.dumps({"status": result["status"], "cases": [{"case": c["case"], "status": c["status"],
        "orders": len(c["orders"]), "fills": len(c["fills"])} for c in result["cases"]], "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
