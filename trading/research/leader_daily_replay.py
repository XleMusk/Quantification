"""Fixed, already-inspected daily-leader research comparison on a common cohort.

The cohort uses the last observed session BEFORE the decision window.  It is
never narrowed by future survival, realised fills or returns.  Unknown cohort
coverage keeps NO_CHANGE; known suspension bars remain known observations and
the shared Engine applies the actual trading block.  No broker/store writer.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..contracts import RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..settings import DATA_ROOT, ROOT
from .shortline_decision_v2 import (
    ShortlineDecisionFeedV2, fixed_exit_policy, holding_origin, verified_breadth,
)
from .shortline_panel_replay import ShortlinePanelFeed, _config


SCHEMA = "leader_daily_comparison_v2"
START, END = "2026-01-05", "2026-10-09"
SOURCE_CUTOFF = "2026-10-13"
FAMILIES = ("stage1_amount_baseline", "stage2_amount_baseline", "stage3_amount_baseline",
            "trend_leader_20_60", "daily_rebound_proxy")
POOLS = ("common_pre_window_symbol20", "common_pre_window_uncapped")
EXIT_POLICIES = ("complete_empty_and_five_session_exit", "verified_full_market_breadth_defense")
RAW_REQUIRED = ("open", "high", "low", "close", "volume", "amount", "pct_chg", "preclose_for_limits")


def _valid_rows(frame):
    numeric = frame.loc[:, RAW_REQUIRED].apply(pd.to_numeric, errors="coerce")
    valid = (np.isfinite(numeric).all(axis=1)
            & numeric[["open", "high", "low", "close", "preclose_for_limits"]].gt(0).all(axis=1)
            & numeric[["volume", "amount"]].ge(0).all(axis=1)
            & numeric.high.ge(numeric[["open", "close"]].max(axis=1))
            & numeric.low.le(numeric[["open", "close"]].min(axis=1)))
    if "source_row_valid" in frame:
        valid &= frame.source_row_valid.astype(bool)
    return valid


def freeze_cohorts(raw, start):
    """Use observable pre-window raw membership and fixed liquidity, no future."""
    available = raw.loc[raw.date.lt(start)]
    if available.empty:
        raise ValueError("Common cohort requires an observed pre-window source session")
    anchor = str(available.date.max())
    rows = available.loc[available.date.eq(anchor)].copy()
    eligible = (rows.symbol.str.match(r"^(?:60\d{4}\.SSE|68\d{4}\.SSE|00\d{4}\.SZSE|30\d{4}\.SZSE)$")
                & _valid_rows(rows) & rows.volume.gt(0) & rows.amount.ge(20_000_000)
                & ~rows.is_st.astype(bool))
    symbols = sorted(rows.loc[eligible, "symbol"].unique())
    if not symbols:
        raise ValueError("No common pre-window observed liquid ordinary-equity cohort")
    return {"anchor": anchor, "available_at": anchor + "T15:05:00+08:00",
            "selection": "anchor valid raw bar, ordinary SH/SZ, non-ST, volume>0, amount>=20m; symbol ascending",
            "future_survival_used": False, "future_candidates_used": False,
            "pools": {POOLS[0]: symbols[:20], POOLS[1]: symbols}}


def dated_coverage(raw, calendar, symbols):
    selected = raw.loc[raw.symbol.isin(symbols)]
    groups = {day: group for day, group in selected.groupby("date", sort=False)}
    result = {}
    for day in calendar:
        rows = groups.get(day, selected.iloc[:0])
        observed = set(rows.symbol)
        valid = set(rows.loc[_valid_rows(rows), "symbol"])
        missing, invalid = sorted(set(symbols) - observed), sorted(observed - valid)
        suspended = sorted(rows.loc[rows.volume.eq(0), "symbol"])
        zero_halts = sorted(rows.loc[rows.volume.eq(0) & rows[["open", "high", "low", "close"]].eq(0).all(axis=1), "symbol"])
        st_unknown = sorted(rows.loc[rows.is_st_unknown, "symbol"]) if "is_st_unknown" in rows else []
        result[day] = {"day": day, "expected_cohort_rows": len(symbols), "observed_cohort_rows": len(observed),
                       "missing_symbols": missing, "invalid_symbols": invalid,
                       "known_suspended_symbols": suspended,
                       "known_halt_zero_ohlc_symbols": zero_halts, "st_unknown_symbols": st_unknown,
                       "status": "complete" if not missing and not invalid else "incomplete",
                       "basis": "frozen pre-window provider-observed cohort; no PIT exchange roster",
                       "known_suspension_is_not_missing_row": True}
    return result


class _DayRows:
    def __init__(self, raw):
        self.groups = {day: rows.set_index("symbol") for day, rows in raw.groupby("date", sort=False)}
        self.cache = {}
    def get(self, day, default=None):
        if day not in self.groups:
            return default
        if day not in self.cache:
            self.cache[day] = self.groups[day].to_dict("index")
        return self.cache[day]


class CommonCohortRawFeed(ShortlinePanelFeed):
    """Same raw feed surface, efficiently indexed; no imputed suspension bars."""
    def __init__(self, raw, calendar, symbols):
        self.symbols, self.calendar, self.signals = list(symbols), list(calendar), {}
        selected = raw.loc[raw.symbol.isin(symbols) & raw.date.isin(calendar)].copy()
        selected["preclose"] = selected["preclose_for_limits"]
        selected["isST"] = selected.is_st.astype(int)
        selected["tradestatus"] = (selected.volume.gt(0) & selected.open.gt(0)).astype(int)
        selected["adjustflag"] = "3"
        self.rows = _DayRows(selected)
        self.volumes = selected.pivot(index="date", columns="symbol", values="volume").reindex(index=calendar, columns=symbols)
        origins = raw.loc[raw.symbol.isin(symbols)].groupby("symbol").date.min().to_dict()
        self.instruments = {symbol: {"ipoDate": origins[symbol], "outDate": ""} for symbol in symbols}
        self.actions, self.instrument_profiles = [], {}
        self.manifest = {"replay_only": True, "scope": "common_pre_window_source_cohort",
                         "source_coverage_scope": "pinned_provider_observed_universe",
                         "listing_date_basis": "first observed raw bar is a lower bound, not verified IPO identity",
                         "corporate_actions_verified": False, "pit_qualified": False, "execution_admitted": False}


class CommonCohortDecisionFeed(ShortlineDecisionFeedV2):
    """Use strict dated cohort decision coverage, but relevant next raw bars.

    Source/cohort incomplete batches stay incomplete.  A missing future raw
    bar of an unheld/noncandidate member is not a future-survival selection
    filter; it is separately recorded in the following day's coverage.
    """
    def __init__(self, base, batches, state, *, market_defense=False):
        super().__init__(base, batches, market_defense=market_defense)
        self.state = state
        self._required = set()
    def signal_frame(self, day, config, context):
        candidates = self.batches.get(day, {}).get("candidates", [])
        held = {symbol for symbol, lots in self.state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        pending = set(self.state.get("pending_targets") or {})
        self._required = held | pending | {row["symbol"] for row in candidates}
        return super().signal_frame(day, config, context)
    def _raw_panel_complete(self, day):
        rows = self.base.rows.get(day, {})
        if not rows:
            return False
        for symbol in self._required:
            row = rows.get(symbol)
            if row is None or any(not np.isfinite(float(row[field])) for field in
                                  ("open", "high", "low", "close", "preclose", "volume", "amount")):
                return False
            if any(row[field] <= 0 for field in ("open", "high", "low", "close", "preclose")):
                return False
        return True


def scope_batches(source_batches, coverage, symbols, raw, *, decision_end=None, status_evidence=None,
                  status_event_evidence=None):
    """Complete only when BOTH declared source batch and cohort are observed."""
    output, observations = {}, []
    members = set(symbols)
    raw_index = raw.loc[raw.symbol.isin(members) & raw.date.isin(coverage)].set_index(["date", "symbol"])
    for day, facts in coverage.items():
        original = source_batches.get(day)
        if original is None:
            continue
        batch = deepcopy(original)
        if status_event_evidence is not None:
            from .dated_nontrading_coverage import apply_status_event_eligibility
            batch = apply_status_event_eligibility(batch, status_event_evidence)
        known_source = original.get("status") in {"complete", "observed_source_complete"}
        candidate_facts = None
        if status_evidence is not None and day in status_evidence:
            from .dated_nontrading_coverage import candidate_coverage
            # This is an explicit retrospective diagnostic input. It only
            # explains absent nontrading candidates, never a held raw mark.
            # CommonCohortDecisionFeed still checks current/next held, pending
            # and selected-candidate prices independently for each account.
            candidate_facts = candidate_coverage(facts, status_evidence[day])
            batch["candidate_coverage"] = candidate_facts
        candidate_status = candidate_facts["status"] if candidate_facts is not None else facts["status"]
        if decision_end is not None and day > decision_end:
            batch.update(status="incomplete", candidates=[], incomplete_reason="execution_tail_no_new_decision")
        elif not known_source or candidate_status != "complete":
            batch["status"] = "incomplete"
            batch["incomplete_reason"] = "source_batch_incomplete" if not known_source else "frozen_cohort_raw_coverage_incomplete"
        else:
            # Filtering uses the declared cohort and decision-day known status,
            # not following prices, realised fills, labels or survival.
            candidates = []
            for row in batch.get("candidates", []):
                if row["symbol"] not in members:
                    continue
                if candidate_facts is not None and row["symbol"] in candidate_facts["candidate_nontrading_observed_symbols"]:
                    continue
                observed = raw_index.loc[(day, row["symbol"])]
                if not bool(observed.is_st) and observed.volume > 0 and observed.amount >= 20_000_000:
                    candidates.append(row)
            batch.update(status="complete", candidates=candidates[:5])
        batch["cohort_coverage"] = facts
        batch["decision_scope"] = "frozen_pre_window_cohort_only"
        output[day] = batch
        observation = {"day": day, "source_status": original.get("status"),
                       "cohort_status": facts["status"], "decision_status": batch["status"],
                       "selected_candidate_count": len(batch.get("candidates", [])) if batch["status"] == "complete" else None}
        if candidate_facts is not None:
            observation["candidate_coverage_status"] = candidate_facts["status"]
            observation["candidate_nontrading_observed_count"] = candidate_facts["candidate_nontrading_observed_count"]
        if status_event_evidence is not None:
            observation["new_candidate_status_event_excluded_rows"] = batch["dated_status_event_eligibility"]["new_candidate_excluded_rows"]
            observation["status_event_raw_coverage_repaired"] = False
        observations.append(observation)
    return output, observations


def _case(base, batches, family, pool, policy, start, end, *, replay_risk_exit_policy=None, context=None):
    market_defense = policy == EXIT_POLICIES[1]
    if market_defense and not any(batch.get("status") == "complete" and verified_breadth(batch) is not None
                                  for batch in batches.values()):
        return {"family": family, "pool": pool, "exit_policy": policy,
                "status": "unavailable_verified_full_market_breadth", "metrics": None,
                "decision_coverage": {"source_proxy_not_exchange_verified": True},
                "reason": "Provider-inventory breadth is retained as a proxy; verified=false is never promoted to true."}, None
    if context is None:
        context = RunContext(f"leader-daily:{pool}:{family}:{policy}", TradingMode.REPLAY)
    if not isinstance(context, RunContext) or context.mode != TradingMode.REPLAY:
        raise ValueError("Historical diagnostic requires a replay RunContext")
    config, state = _config(100000, 5), new_state(100000)
    feed = CommonCohortDecisionFeed(base, batches, state, market_defense=market_defense)
    daily, decisions = [], []
    error = None
    for index, day in enumerate(base.calendar):
        trial = deepcopy(state)
        feed.state = trial
        policy_fn = fixed_exit_policy(feed, trial, config, max_holding=True)
        try:
            row = advance(trial, feed, day, config, index, target_policy=policy_fn, context=context,
                          replay_risk_exit_policy=replay_risk_exit_policy)
        except (ValueError, KeyError) as failure:
            error = f"{type(failure).__name__}: {failure}"
            break
        state = trial
        daily.append(row)
        observed = batches.get(day, {})
        no_decision = next((event for event in row["events"] if event["kind"] == "no_decision"), None)
        decisions.append({"day": day, "in_window": start <= day <= end,
                          "batch_status": observed.get("status", "missing"),
                          "candidate_count": len(observed.get("candidates", [])) if observed.get("status") == "complete" else None,
                          "no_change": no_decision is not None, "reason": no_decision.get("reason") if no_decision else None,
                          "actual_holdings": row["holdings"], "stale_symbols": row["stale_symbols"],
                          "held_over_five_sessions": [symbol for symbol, lots in state["positions"].items()
                              if sum(lot["qty"] for lot in lots) > 0 and holding_origin(state, symbol) is not None
                              and index - base.calendar.index(holding_origin(state, symbol)) + 1 > 5]})
    in_window = [row for row in daily if start <= row["date"] <= end]
    observed_days = [row for row in decisions if row["in_window"]]
    blocked = [event for event in state["events"] if event["kind"] == "order_blocked"]
    summary = {"family": family, "pool": pool, "exit_policy": policy,
               "status": "blocked_engine_replay" if error else "completed_already_inspected_engine_diagnostic",
               "metrics": metrics(in_window, 100000) if in_window and not error else None,
               "tail_included_diagnostic_metrics": metrics(daily, 100000) if daily and not error else None,
               "metrics_basis": "same fixed already-inspected window; incomplete/stale observations do not establish strategy validity",
               "decision_coverage": {"expected_window_sessions": sum(start <= day <= end for day in base.calendar),
                                     "processed_window_sessions": len(observed_days),
                                     "complete_batch_days": sum(row["batch_status"] == "complete" for row in observed_days),
                                     "no_change_days": sum(row["no_change"] for row in observed_days),
                                     "no_change_ratio": sum(row["no_change"] for row in observed_days) / len(observed_days) if observed_days else None,
                                     "missing_or_incomplete_batch_days": sum(row["batch_status"] != "complete" for row in observed_days),
                                     "incomplete_batch_held_days": sum(row["batch_status"] != "complete" and row["actual_holdings"] > 0 for row in observed_days),
                                     "empty_complete_batch_held_days": sum(row["candidate_count"] == 0 and row["actual_holdings"] > 0 for row in observed_days),
                                     "over_five_session_held_days": sum(bool(row["held_over_five_sessions"]) for row in observed_days)},
               "orders": len(state["orders"]), "fills": len(state["fills"]),
               "blocked_orders": len(blocked), "blocked_exit_count": sum(event.get("side") == "sell" for event in blocked),
               "open_positions": {symbol: sum(lot["qty"] for lot in lots) for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)},
               "last_successful_session": state["day"], "error": error}
    reporting_boundaries(summary)
    return summary, {"state": state, "daily": daily, "decisions": decisions}


def reporting_boundaries(summary):
    """Cash held because decisions are unknown is not a zero-return strategy."""
    coverage = summary.get("decision_coverage", {})
    if coverage.get("complete_batch_days") == 0:
        summary["engine_no_change_account_metrics"] = summary.get("metrics")
        summary.update(status="unavailable_complete_cohort_decision_batches", metrics=None,
                       reason="Every declared-cohort decision batch is incomplete; the cash-only Engine trace is not strategy performance.")
    return summary


def export_data_blockers(coverage, out):
    """Small coverage/root-cause view; no symbol removal or new status facts."""
    from collections import Counter
    out = Path(out)
    rows = []
    frequencies = Counter()
    for facts in coverage:
        if not START <= facts["day"] <= END:
            continue
        frequencies.update(facts["missing_symbols"])
        rows.append({"day": facts["day"], "expected_count": facts["expected_cohort_rows"],
                     "observed_count": facts["observed_cohort_rows"], "missing_count": len(facts["missing_symbols"]),
                     "invalid_count": len(facts["invalid_symbols"]),
                     "known_suspended_count": len(facts["known_suspended_symbols"]),
                     "known_halt_zero_ohlc_count": len(facts["known_halt_zero_ohlc_symbols"]) if "known_halt_zero_ohlc_symbols" in facts else None,
                     "st_unknown_count": len(facts["st_unknown_symbols"]) if "st_unknown_symbols" in facts else None,
                     "missing_symbols_first20": "|".join(facts["missing_symbols"][:20]),
                     "invalid_symbols": "|".join(facts["invalid_symbols"]),
                     "status": facts["status"]})
    pd.DataFrame(rows).to_csv(out / "data_blocker_daily.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"symbol": symbol, "true_missing_row_days": count, "window_sessions": len(rows),
                   "missing_ratio": count / len(rows), "reason": "no row; suspension/delist/ST status not inferred"}
                  for symbol, count in frequencies.most_common(20)]).to_csv(out / "data_blocker_missing_frequency_top20.csv", index=False, encoding="utf-8-sig")


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def compare_bundle(bundle, out, *, start=START, end=END, data_out=None, status_evidence=None, tail_metadata=None):
    """Compare the predeclared families/scopes without looking at a winner."""
    if tail_metadata is not None:
        from .leader_tail_overlay import validate_tail_metadata
        validate_tail_metadata(bundle, tail_metadata)
        if tail_metadata["decision_end"] != end:
            raise ValueError("Research tail metadata and fixed decision cutoff disagree")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    data_out = Path(data_out) if data_out else DATA_ROOT / "research/leader_daily_replay" / out.name
    data_out.mkdir(parents=True, exist_ok=False)
    raw = bundle["raw_bars"].copy()
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Leader raw source has duplicate dated securities")
    cohort = freeze_cohorts(raw, start)
    calendar = [day for day in bundle["calendar"] if start <= day <= end]
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("Leader comparison requires a unique declared decision calendar")
    # Preserve declared exchange sessions even when the first tail session is
    # missing in raw source.  Do not silently turn a later observed open into
    # the first following exchange open.
    tail = [day for day in bundle["calendar"] if end < day <= str(raw.date.max())]
    replay_calendar = calendar + tail
    snapshot_end = str(raw.date.max())
    _write_json(data_out / "cohort.json", cohort)
    cohort_summary = {key: value for key, value in cohort.items() if key != "pools"}
    cohort_summary.update(artifact=str(data_out / "cohort.json"), pools={pool: len(symbols) for pool, symbols in cohort["pools"].items()})
    protocol = {"schema": "leader_daily_comparison_protocol_v2", "start": start, "end": end,
                "execution_tail_sessions": tail, "snapshot_end": snapshot_end,
                "families": list(FAMILIES), "pools": list(POOLS), "exit_policies": list(EXIT_POLICIES),
                "expected_case_count": 20, "cohort": cohort_summary, "config": _config(100000, 5).to_dict(),
                "max_holding_sessions": 5, "complete_empty_target": "cash", "incomplete_target": "no_change",
                "selection_after_pool_filter": "preserve producer fixed ranking; top5; no future path filter",
                "window_already_inspected": True, "future_independent_40_session_window_run": False,
                "verified_breadth_requirement": "Same-date complete verified full-market counts; provider proxies remain unverified and unavailable",
                "symbol_cap_bias": "Symbol-ascending anchor20 is a fixed engineering sample; never choose the profitable cap or compare to old first_seen panels as the same universe",
                "parameters_tuned": False, "model_refit": False, "future_labels_used_for_score": False,
                "execution_admitted": False}
    if status_evidence is not None:
        protocol["optional_dated_nontrading_coverage"] = {
            "days": sorted(status_evidence), "temporal_basis": "retrospective_retrieval_not_point_in_time",
            "candidate_coverage_only": True, "raw_marks_and_held_checks_unchanged": True,
            "pit_verified": False, "execution_admitted": False}
    if tail_metadata is not None:
        protocol["optional_research_tail_overlay"] = tail_metadata
    # Protocol is materialized before any Engine case has been evaluated.
    _write_json(out / "protocol.json", protocol)
    _write_json(data_out / "protocol.json", protocol)
    cases = []
    for pool in POOLS:
        symbols = cohort["pools"][pool]
        coverage = dated_coverage(raw, replay_calendar, symbols)
        _write_json(data_out / f"{pool}_coverage.json", list(coverage.values()))
        if pool == POOLS[1]:
            export_data_blockers(list(coverage.values()), out)
        base = CommonCohortRawFeed(raw, replay_calendar, symbols)
        for family in FAMILIES:
            batches, source_coverage = scope_batches(bundle["batches"][family], coverage, symbols, raw,
                                                      decision_end=end, status_evidence=status_evidence)
            for policy in EXIT_POLICIES:
                row, evidence = _case(base, batches, family, pool, policy, start, end)
                row.update(cohort_size=len(symbols), candidate_panel_only=True, strategy_validated=False,
                           selection_enabled=False, execution_admitted=False)
                case_out = data_out / f"{pool}__{family}__{policy}"
                case_out.mkdir()
                _write_json(case_out / "summary.json", row)
                _write_json(case_out / "decision_batches.json", batches)
                _write_json(case_out / "source_and_cohort_coverage.json", source_coverage)
                if evidence is not None:
                    _write_json(case_out / "account.json", evidence["state"])
                    pd.DataFrame([{key: value for key, value in day.items() if key != "events"} for day in evidence["daily"]]).to_csv(case_out / "daily.csv", index=False)
                    pd.DataFrame(evidence["decisions"]).to_csv(case_out / "decisions.csv", index=False)
                    pd.DataFrame(evidence["state"]["orders"].values()).to_csv(case_out / "orders.csv", index=False)
                    pd.DataFrame(evidence["state"]["fills"].values()).to_csv(case_out / "fills.csv", index=False)
                row["artifacts"] = str(case_out)
                cases.append(row)
                print(f"{pool} {family} {policy}: {row['status']}", flush=True)
    result = {"schema": SCHEMA, "status": "fixed_comparison_with_engine_failures" if any(row["status"] == "blocked_engine_replay" for row in cases) else "fixed_already_inspected_comparison_complete", "start": start, "end": end,
              "snapshot_end": snapshot_end, "snapshot": bundle.get("source_metadata", {}).get("snapshot"), "window_already_inspected": True,
              "case_count": len(cases), "cases": cases, "cohort": cohort_summary, "protocol": str(out / "protocol.json"), "artifacts": str(data_out),
              "source_coverage_scope": "pinned_provider_observed_universe", "source_verified": False,
              "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
              "execution_admitted": False, "persistent_ledger_mutated": False,
              "model_refit": False, "future_labels_used_for_score": False,
              "future_independent_40_session_window_run": False,
              "limitations": ["Provider observations do not prove a PIT exchange roster or corporate-action completeness.",
                              "The old window is already inspected; diagnostic net returns cannot promote a strategy.",
                              "Missing frozen-cohort rows retain NO_CHANGE; known suspensions and failed exits remain in Ledger evidence.",
                              "Bounded and uncapped cases are both retained even when a scope spends most sessions in NO_CHANGE.",
                              "Unverified provider breadth is a diagnostic proxy; blocked defense cases get no invented return."]}
    if tail_metadata is not None:
        result["price_source_mode"] = "mixed_source_research_tail_overlay"
        result["optional_research_tail_overlay"] = tail_metadata
    _write_json(out / "result.json", result)
    pd.DataFrame([{key: row.get(key) for key in ("family", "pool", "exit_policy", "status", "cohort_size", "orders", "fills", "blocked_orders", "blocked_exit_count", "artifacts")}
                  for row in cases]).to_csv(out / "summary.csv", index=False)
    write_report(out, result, protocol)
    return result


def write_report(out, result, protocol):
    """Render all retained receipts without running an Engine case again."""
    out = Path(out)
    start, end, snapshot_end = result["start"], result["end"], result["snapshot_end"]
    tail, cases = protocol["execution_tail_sessions"], result["cases"]
    lines = ["# 日线龙头策略 V2 系统诊断", "", f"决策窗 {start} 至 {end}；源至 {snapshot_end}，执行尾部 {tail}。",
             "本窗口已经检查过，不是独立检验。先固定共同前窗 cohort、5 日退出与费用，再运行 20 个声明的 case；不选盈利版本。", "",
             "| 策略 | 样本 | 退出 | 状态 | 净收益 | NO_CHANGE | 订单/成交 | 阻断退出 | 未退出持仓 |",
             "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for row in cases:
        value = row.get("metrics")
        ratio = row["decision_coverage"].get("no_change_ratio")
        lines.append(f"| {row['family']} | {row['pool']} | {row['exit_policy']} | {row['status']} | "
                     f"{format(value['net_return'], '.2%') if value else '不可用'} | {format(ratio, '.2%') if ratio is not None else '不可用'} | "
                     f"{row.get('orders', '--')}/{row.get('fills', '--')} | {row.get('blocked_exit_count', '--')} | {len(row['open_positions']) if 'open_positions' in row else '--'} |")
    lines += ["", "解释收益前先看有效决策覆盖、NO_CHANGE、成交、阻断和残仓；固定20符号样本有样本偏差，不能替代全cohort结果。",
              "股票清空目标仍受 T+1、停牌、涨跌停和量能限制；缺行情不能当已清仓。",
              "数据为 provider observed inventory，PIT、公司行动与交易所全覆盖未验证；广度防守缺 verified full-market 契约的 case 不输出收益。",
              "execution_admitted=false；没有客户端、持久账户或真实委托操作。"]
    if "optional_dated_nontrading_coverage" in protocol:
        lines += ["", "本次显式启用同日 provider 状态的事后覆盖诊断：仅缺行且未持有的停牌候选可解释为非交易观察。",
                  "原始行情覆盖仍保留缺失；持仓、待执行与候选价格要求不变。回取状态不证明当时可得或 PIT 资格。"]
    if "optional_research_tail_overlay" in protocol:
        overlay = protocol["optional_research_tail_overlay"]
        lines += ["", f"本次使用混合来源研究价格视图：在 {overlay['added_day']} 仅追加 {overlay['added_rows']} 条可用原始行情，未覆盖 StockDB 或重算历史信号。",
                  f"{len(overlay['skipped_unusable_tail_rows'])} 条停牌库存记录因活动字段缺失未转成 Engine 行；完整 cohort 该日仍缺 {overlay['full_cohort_tail_missing_count']} 条行情。"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--tail-package", type=Path,
                        help="Explicit separate common20 dailyplus raw tail package; never replaces StockDB rows")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if (args.start, args.end) != (START, END):
        parser.error("This consumer only runs the declared already-inspected 2026-01-05..2026-10-09 diagnostic; future independent window is not admitted")
    from .leader_daily_signals import produce_leader_daily_batches
    bundle = produce_leader_daily_batches(args.snapshot, args.start, SOURCE_CUTOFF)
    tail_metadata = None
    if args.tail_package is not None:
        from .leader_tail_overlay import apply_tail_overlay
        bundle, tail_metadata = apply_tail_overlay(bundle, args.tail_package)
    out = args.out or ROOT / "outputs/leader_daily_replay" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    result = compare_bundle(bundle, out, start=args.start, end=args.end, tail_metadata=tail_metadata)
    print(out, flush=True)
    return 0 if len(result["cases"]) == 20 and result["status"] == "fixed_already_inspected_comparison_complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
