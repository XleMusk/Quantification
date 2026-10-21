"""Fixed research applicability and exits for the eight descriptive leader types.

The two-close guard is an unvalidated comparison hypothesis. It neither changes
the original descriptive labels nor admits a current trading signal.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math

from .leader_environment import ARCHETYPES

LOCAL = timezone(timedelta(hours=8))
SCHEMA = "leader_eight_type_environment_policy_v1"
STRENGTH = "broad_strength_snapshot"
MIXED = "mixed_breadth_snapshot"
WEAKNESS = "broad_weakness_snapshot"
PHASES = {STRENGTH, MIXED, WEAKNESS, "unknown"}
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
         "selection_enabled": False, "execution_admitted": False,
         "current_signal_admitted": False, "historical_publication_verified": False}


def _spec(phases, observation, requirements, invalidation):
    return {"entry_phases_after_two_close_confirmation": phases,
            "observation_conditions": observation,
            "additional_candidate_requirements": requirements,
            "invalidation_signals": invalidation}


TYPE_RULES = {
    "space_height_leader": _spec([STRENGTH], "同板制已知梯队高度与市场接力扩散共同观察",
        ["IPO及涨跌停参考链明确", "同板未知高度不能忽略", "次日可成交性独立核验"],
        ["自身断板", "同板更高已知梯队出现", "环境普跌立即暂停新增"]),
    "relay_continuation_leader": _spec([STRENGTH], "准确相邻日连板延续，避免把单日强势当接力",
        ["准确日历与连板参考链", "板块共振证据", "封单和次日开盘成交条件不能由日线推定"],
        ["准确后继不再连板", "板块扩散收缩", "次日一字不可买或跌停不可卖"]),
    "trend_leader": _spec([STRENGTH, MIXED], "趋势结构、市场及行业相对强弱共同观察",
        ["原20/60观察行趋势规则保留", "精确交易日窗口缺口单独披露", "参考价与行动边界明确"],
        ["跌破原SMA20结构", "SMA20不再高于SMA60", "相对强弱持续转负"]),
    "liquidity_capacity_proxy": _spec([STRENGTH, MIXED], "原成交额前1%容量代理，结合广度与集中度观察",
        ["完整当日成交额分母", "真实流通盘与换手仍另核", "不能由成交额推出净流入"],
        ["成交额分位回落", "放量同时广度和价格承接转弱", "失守预先记录结构价位"]),
    "cross_regime_candidate": _spec([STRENGTH, MIXED], "普跌时保留观察，环境确认恢复后才允许固定研究候选",
        ["episode开始前冻结身份", "首次episode的三个准确相邻收盘", "两段行业相对抗跌", "确认只在第三收盘证据可得后发生"],
        ["失守episode预先冻结参考结构", "两段相对韧性不成立", "行业或参考链缺失时保持未知而非判失败"]),
    "rebound_repair_candidate": _spec([MIXED], "普跌后观察修复，在分化环境连续确认后检验",
        ["原D-2封板/D-1阴线/D收复前开盘价规则", "三日准确参考链", "不把修复代理当回封盘口证明"],
        ["再度失守修复参考价", "修复后市场重新普跌", "修复量价或行业支撑消失"]),
    "sector_catchup_leader": _spec([STRENGTH, MIXED], "有时点的宽行业先行股与滞后首板修复代理",
        ["当时可得行业成员", "前日先行股身份", "前日落后而当日超越行业", "行业上涨与准确首板链"],
        ["先行股结构破坏", "行业转弱", "失守候选启动低点", "旧行业分类不能证明当日题材"]),
    "turnover_leader": _spec([STRENGTH, MIXED], "真实日换手增加且非一字的封板代理",
        ["D及D-1同口径百分比换手", "准确封板资格与参考链", "日内分歧回封及承接仍需独立证据"],
        ["换手退潮同时弱收盘", "高换手但封板或结构失败", "失守预先记录启动低点"]),
}


def _aware(value):
    if not isinstance(value, str):
        raise ValueError("Explicit timezone-aware observation clock required")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Explicit timezone-aware observation clock required")
    return stamp.astimezone(LOCAL)


def classify_snapshot(breadth, median_return_pct):
    if (type(breadth) not in (int, float) or type(median_return_pct) not in (int, float)
            or not math.isfinite(breadth) or not math.isfinite(median_return_pct)
            or not -1 <= breadth <= 1):
        return "unknown"
    if breadth >= .2 and median_return_pct > 0:
        return STRENGTH
    if breadth <= -.2 and median_return_pct < 0:
        return WEAKNESS
    return MIXED


def evaluate_environment_path(observations, calendar, as_of):
    """Return the fixed guard for the latest supplied close.

    Each observation is {day, available_at, source_complete, breadth,
    median_return_pct, optional phase}. Calendar is the pinned session list.
    available_at is actual evidence availability, never a historical backdate.
    """
    observed = _aware(as_of)
    sessions = list(calendar)
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("Pinned ordered unique exchange sessions required")
    for value in sessions:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Canonical exchange sessions required")
    if not observations:
        raise ValueError("At least one saved close observation required")
    days, clocks, classified = [], [], []
    for item in observations:
        day = item["day"]
        if day not in sessions:
            raise ValueError("Observation is outside pinned exchange sessions")
        clock = _aware(item["available_at"])
        if clock < _aware(day + "T15:00:00+08:00") or clock > observed:
            raise ValueError("Close availability is intraday or future to actual observation")
        phase = (classify_snapshot(item.get("breadth"), item.get("median_return_pct"))
                 if item.get("source_complete") is True else "unknown")
        if item.get("phase", phase) not in PHASES or item.get("phase", phase) != phase:
            raise ValueError("Claimed phase differs from fixed close evidence")
        days.append(day); clocks.append(clock); classified.append(phase)
    if days != sorted(set(days)):
        raise ValueError("Close observations must be unique and ordered")
    day, phase = days[-1], classified[-1]
    index = sessions.index(day)
    previous_required = sessions[index - 1] if index else None
    adjacent = len(days) >= 2 and days[-2] == previous_required
    confirmed = adjacent and phase != "unknown" and classified[-2] == phase
    if phase == WEAKNESS:
        mode, reason = "EXIT_ALL", "known_weakness_independent_next_open_risk_exit"
    elif phase == "unknown":
        mode, reason = "NO_CHANGE", "latest_close_evidence_unknown"
    elif not confirmed:
        mode, reason = "NO_CHANGE", "awaiting_two_exact_adjacent_same_phase_closes"
    else:
        mode, reason = "ALLOW_FIXED_CANDIDATES", "two_exact_adjacent_closes_confirmed"
    decisions = {}
    for identity, name in ARCHETYPES:
        rule = TYPE_RULES[identity]
        type_mode, type_reason = mode, reason
        if mode == "ALLOW_FIXED_CANDIDATES" and phase not in rule["entry_phases_after_two_close_confirmation"]:
            type_mode, type_reason = "EXIT_ALL", "confirmed_phase_not_applicable_to_fixed_type"
        decisions[identity] = {"id": identity, "name": name, "mode": type_mode,
            "reason": type_reason, "rule": rule, "candidate_evidence_still_required": True, **FLAGS}
    return {"schema": SCHEMA, "day": day, "as_of": observed.isoformat(), "phase": phase,
        "mode": mode, "reason": reason, "previous_session_required": previous_required,
        "previous_supplied_day": days[-2] if len(days) >= 2 else None,
        "exact_previous_present": adjacent, "two_close_confirmation": confirmed,
        "latest_inputs_available_at": max(clocks).isoformat(),
        "execution_not_before": sessions[index + 1] if index + 1 < len(sessions) else None,
        "per_type": decisions, "parameters_selected_by_PnL": False,
        "unknown_candidate_priority": "independent_known_environment_exit_before_entry_unknown_NO_CHANGE",
        "semantics": "fixed_research_guard_not_validated_regime_or_leader_selector",
        **FLAGS}
