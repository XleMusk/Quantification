"""Fixed type-by-environment retrospective cases from saved eight-type inputs.

This module reads the existing dated evidence. It does not classify new leaders,
run a strategy, contact a provider, or infer a personal trade or account return.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from .baostock_daily_slice import FIELDS, digest
from .leader_case_study import _clean, _write
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import TYPE_IDS, load_inputs
from .leader_environment_policy import MIXED, STRENGTH, WEAKNESS, TYPE_RULES, evaluate_environment_path


SCHEMA = "leader_type_phase_case_learning_manifest_v1"
CASE_SCHEMA = "leader_type_phase_retrospective_case_v1"
PROTOCOL_SCHEMA = "leader_type_phase_case_learning_protocol_v1"
METHOD_ID = "type_phase_first2_distinct_symbol_by_day_symbol_v1"
PROTOCOL = {
    "selection": METHOD_ID,
    "fallback": "only_if_cell_has_zero_matched_first1_unresolved",
    "window_offsets": [-2, -1, 0, 1, 2],
    "phases": [WEAKNESS, MIXED, STRENGTH, "unknown"],
    "matched_distinct_symbols_per_type_phase": 2,
    "unresolved_observations_per_zero_matched_cell": 1,
    "order": ["day", "canonical_symbol"],
    "scope": "original_input_fixed_scope_members_only",
    "retrospective_only": True,
    "future_outcomes_or_coverage_used_for_selection": False,
    "replacement_after_raw_or_qualification_failure": False,
    "known_not_matched_is_not_unknown": True,
    "raw_path": "only_load_inputs_returned_exact_dated_raw_no_history_extension",
    "same_symbol_across_types_or_phases_is_not_independent": True,
}
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "orders_generated": False, "targets_generated": False,
    "ledger_mutated": False, "account_created": False, "engine_advanced": False,
    "provider_contacted": False, "personal_blotter_reproduced": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "historical_signal_publication_verified": False, "corporate_actions_verified": False,
    "pit_verified": False, "future_outcomes_used_for_sample_selection": False}
TYPE_NAMES = dict(zip(TYPE_IDS, ("空间龙", "接力龙", "趋势龙", "容量龙", "穿越龙", "反包龙", "补涨龙", "换手龙")))
PHASE_NAMES = {WEAKNESS: "弱市截面", MIXED: "混合截面", STRENGTH: "强市截面", "unknown": "环境未知"}
FEATURE_FIELDS = ("name", "board", "is_st", "is_st_unknown", "stage", "stage_reference_known",
    "limit_up", "daily_rebound", "calendar_contiguous", "listing_qualification_status",
    "listing_feature_reason", "rebound_feature_reason", "feature_decision_as_of",
    "listing_feature_method_id", "sma20", "sma60", "ret20", "ret60", "observed_history_rows")
CANDIDATE_FIELDS = ("symbol", "name", "date", "source_day", "board", "amount_cny", "pct_chg",
    "stage", "amount_rank_pct", "available_at", "setup_low", "structure_high", "evidence",
    "invalidation", "ipo_feature_support", "identity_status", "trade_ready", "confirmed_leader")
QUESTIONS = {
    "space_height_leader": "同板制的已知局部高度在准确后继收盘是否延续？若参考链或同板高度未知，不能判定被超越或断板。",
    "relay_continuation_leader": "准确后继的已保存资格与连板链是否仍成立，还是仅价格强势？日线不能回答封单或次日可买数量。",
    "trend_leader": "其当日SMA20/SMA60及观察行趋势条件在后继何时失效？观察行不是完整交易日窗口，旧行业亦不证明题材共振。",
    "liquidity_capacity_proxy": "成交额容量代理在不同环境中的后继价格路径有何差别？成交额高不能推出净流入、真实换手或收益。",
    "cross_regime_candidate": "实际登记早于哪个环境变更确认收盘，首个episode能否取得三个准确收盘？未完成前只保留身份与抗跌证据缺口。",
    "rebound_repair_candidate": "D-2封板、D-1回落和D收复的准确参考链是否齐全？后继是否失守当时修复结构，不能用后见上涨补齐缺证。",
    "sector_catchup_leader": "前日已知先行身份与滞后行业成员在当时是否可用？旧宽行业中相对领先不等于当天题材补涨已确认。",
    "turnover_leader": "D与D-1同口径百分比换手是否均可得并增加？非一字日线不能证明盘中分歧回封或持有人承接。",
}


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Actual timezone-aware recording clock required")
    return stamp


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(SSE|SZSE)", value):
        raise ValueError("Canonical symbol required; never silently drop a member")
    return value


def _partition(item, members):
    candidates = item.get("candidates")
    unresolved = item.get("unresolved_symbols")
    exclusions = item.get("known_exclusions")
    if not isinstance(candidates, list) or not isinstance(unresolved, list) or not isinstance(exclusions, dict):
        raise ValueError("Saved matched, unresolved and known-not-matched partitions required")
    by_symbol = {_symbol(row["symbol"]): row for row in candidates}
    unresolved_set, known_set = {_symbol(s) for s in unresolved}, {_symbol(s) for s in exclusions}
    if len(by_symbol) != len(candidates) or len(unresolved_set) != len(unresolved):
        raise ValueError("Duplicate saved type membership")
    matched_set = set(by_symbol)
    if matched_set & unresolved_set or matched_set & known_set or unresolved_set & known_set:
        raise ValueError("Matched, unresolved and known-not-matched cannot overlap")
    if members - (matched_set | unresolved_set | known_set):
        raise ValueError("Missing type membership cannot be silently counted as known-not-matched")
    expected = "matched" if matched_set else "unknown" if unresolved_set else "not_matched"
    if item.get("status") != expected:
        raise ValueError("Saved aggregate type status disagrees with its full partitions")
    return by_symbol, unresolved_set, known_set


def select_cases(panel_by_day, scope_members, calendar, *, as_of):
    """Read only D-labelled memberships; no raw price or future label input."""
    members = {_symbol(s) for s in scope_members}
    if not members or len(members) != len(scope_members):
        raise ValueError("Nonempty distinct original frozen scope required")
    if not panel_by_day or list(sorted(panel_by_day)) != [d for d in calendar if min(panel_by_day) <= d <= max(panel_by_day)]:
        raise ValueError("Exact continuous dated source panels required")
    observations, environments = [], {}
    cells = {(identity, phase): {"type_id": identity, "type_name": TYPE_NAMES[identity], "phase": phase,
        "source_days": [], "member_date_observations": 0, "matched": 0, "unresolved": 0,
        "known_not_matched": 0, "outside_fixed_scope_count": 0, "daily_counts": [],
        "selected_case_ids": [], "selected": 0, "selected_matched": 0, "selected_unresolved": 0,
        "_matched": [], "_unresolved": []} for identity in TYPE_IDS for phase in PROTOCOL["phases"]}
    for day, panel in sorted(panel_by_day.items()):
        if day != panel.get("day") or [t["id"] for t in panel["types"]] != list(TYPE_IDS):
            raise ValueError("Every day needs eight types in their original order")
        observations.append({"day": day, "available_at": panel["available_at"], **panel["environment"]})
        env = evaluate_environment_path(deepcopy(observations), calendar=calendar, as_of=as_of)
        env["snapshot_inputs"] = deepcopy(panel["environment"])
        environments[day] = env
        for item in panel["types"]:
            matched, unresolved, known = _partition(item, members)
            cell = cells[item["id"], env["phase"]]
            counts = {"day": day, "matched": len(set(matched) & members), "unresolved": len(unresolved & members),
                "known_not_matched": len(known & members), "outside_fixed_scope_count": len((set(matched) | unresolved | known) - members)}
            cell["source_days"].append(day)
            cell["member_date_observations"] += len(members)
            for key in ("matched", "unresolved", "known_not_matched", "outside_fixed_scope_count"):
                cell[key] += counts[key]
            cell["daily_counts"].append(counts)
            cell["_matched"].extend((day, s) for s in sorted(set(matched) & members))
            cell["_unresolved"].extend((day, s) for s in sorted(unresolved & members))
    selected = []
    for cell in cells.values():
        if cell["matched"]:
            choices, seen = [], set()
            for day, symbol in sorted(cell["_matched"]):
                if symbol not in seen:
                    choices.append((day, symbol))
                    seen.add(symbol)
                if len(choices) == 2:
                    break
            kind, reason = "matched_proxy", "first2_distinct_symbols_in_fixed_date_symbol_order"
            coverage = "matched_proxy_examples_selected"
        elif cell["unresolved"]:
            choices = sorted(cell["_unresolved"])[:1]
            kind, reason = "unresolved_observation", "first_unresolved_in_fixed_date_symbol_order"
            coverage = "unresolved_observation_only_no_matched_evidence"
        else:
            choices, kind, reason = [], None, None
            coverage = "known_no_match_in_observed_phase" if cell["source_days"] else "phase_not_observed_in_source_window"
        for day, symbol in choices:
            case_id = "|".join((cell["type_id"], cell["phase"], day, symbol))
            selected.append({"case_id": case_id, "type_id": cell["type_id"], "phase": cell["phase"],
                "day": day, "symbol": symbol, "selection_kind": kind, "selection_reason": reason})
            cell["selected_case_ids"].append(case_id)
            cell["selected_matched" if kind == "matched_proxy" else "selected_unresolved"] += 1
        cell["selected"] = len(choices)
        cell["coverage_status"] = coverage
        cell["matched_and_unresolved_can_coexist"] = bool(cell["matched"] and cell["unresolved"])
        del cell["_matched"], cell["_unresolved"]
    overlaps = {}
    for row in selected:
        overlaps.setdefault(row["symbol"], []).append(row["case_id"])
    for row in selected:
        row["same_symbol_other_case_ids"] = [c for c in overlaps[row["symbol"]] if c != row["case_id"]]
        row["same_symbol_overlap"] = bool(row["same_symbol_other_case_ids"])
        row["independent_sample"] = False
    return {"selected": selected, "coverage_matrix": list(cells.values()), "environments": environments}


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _raw_key(symbol):
    return ("sh." if symbol.endswith(".SSE") else "sz.") + symbol[:6]


def _bar(row):
    if row is None:
        return {"status": "missing_raw_row", "raw": None}
    raw = {key: _clean(row.get(key)) for key in FIELDS.split(",")}
    if raw["adjustflag"] != "3":
        status = "unverified_raw_basis"
    elif raw["tradestatus"] == "0":
        status = "halted_source_row"
    elif raw["tradestatus"] != "1":
        status = "source_trade_status_unknown"
    else:
        values = [_decimal(raw[key]) for key in ("open", "high", "low", "close")]
        status = "observed_active_raw_bar" if all(v is not None and v > 0 for v in values) else "invalid_active_price"
    return {"status": status, "raw": raw}


def _reference(row, previous):
    if row["status"] != "observed_active_raw_bar" or previous["status"] != "observed_active_raw_bar":
        return {"status": "unknown_missing_active_exact_pair", "relative_difference": None}
    preclose, close = _decimal(row["raw"]["preclose"]), _decimal(previous["raw"]["close"])
    if preclose is None or preclose <= 0 or close is None or close <= 0:
        return {"status": "unknown_reference", "relative_difference": None}
    difference = abs(preclose / close - 1)
    return {"status": "consistent_within_one_percent_proxy" if difference <= Decimal("0.01") else "reference_mismatch",
        "relative_difference": str(difference)}


def daily_slots(symbol, day, sessions, raw_index):
    """Five exact calendar slots, never five surviving/active observed rows."""
    origin = sessions.index(day)
    result = []
    e = _bar(raw_index.get((day, _raw_key(symbol))))
    for offset in PROTOCOL["window_offsets"]:
        index = origin + offset
        date = sessions[index] if 0 <= index < len(sessions) else None
        bar = _bar(raw_index.get((date, _raw_key(symbol))))
        previous_date = sessions[index - 1] if 0 < index < len(sessions) else None
        prior = _bar(raw_index.get((previous_date, _raw_key(symbol))))
        reference = _reference(bar, prior)
        below = None
        # Missing or inconsistent intermediate references do not become a price
        # structure failure, nor does a halt become a zero-return observation.
        if offset > 0 and e["status"] == "observed_active_raw_bar" and bar["status"] == "observed_active_raw_bar":
            chain = [_reference(_bar(raw_index.get((sessions[j], _raw_key(symbol)))),
                _bar(raw_index.get((sessions[j - 1], _raw_key(symbol))))) for j in range(origin + 1, index + 1)]
            if all(r["status"] == "consistent_within_one_percent_proxy" for r in chain):
                below = _decimal(bar["raw"]["close"]) < _decimal(e["raw"]["low"])
        result.append({"offset_sessions": offset, "day": date,
            "section": "information_through_E" if offset <= 0 else "after_E_retrospective_path",
            **bar, "previous_exact_session": previous_date,
            "reference_vs_previous_exact_session": reference,
            "close_below_fixed_E_raw_low": below})
    return result


def prepare_cases(loaded, *, recorded_at):
    payload = loaded["calendar_payload"]
    sessions, _, status = _calendar_view(payload, str(payload["year"]) + "-01-01", str(payload["year"]) + "-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Pinned exact exchange calendar required")
    selection = select_cases(loaded["panel_by_day"], loaded["scope_members"], sessions, as_of=recorded_at)
    raw = loaded["raw"]
    if raw.duplicated(["date", "code"]).any() or set(raw.date) - set(loaded["source_days"]):
        raise ValueError("Use only distinct exact raw rows returned by load_inputs")
    raw_index = {(r["date"], r["code"]): r for r in raw.to_dict("records")}
    cases = []
    for selected in selection["selected"]:
        day, symbol, identity = selected["day"], selected["symbol"], selected["type_id"]
        panel = loaded["panel_by_day"][day]
        item = next(t for t in panel["types"] if t["id"] == identity)
        candidate = next((r for r in item["candidates"] if r["symbol"] == symbol), None)
        feature = panel.get("risk_features", {}).get(symbol, {})
        unknown = item.get("unknown_symbols")
        unknown_reason = unknown.get(symbol) if isinstance(unknown, dict) else None
        slots = daily_slots(symbol, day, sessions, raw_index)
        e = next(r for r in slots if r["offset_sessions"] == 0)
        boundaries = ["这是事后固定规则选出的价格/证据学习案，不是盘前身份、已确认龙头、私人交割单或策略验证。",
            "候选的当日字段截止与资料实际晚派生时间分开；未证明历史时点已发布。",
            "仅使用原" + "、".join(loaded["source_days"]) + "窗口所含日线；缺槽不替换成其他有效交易行，不追加旧历史或未来来源。",
            "未验证公司行动或总收益；提供者涨跌幅和原始收盘路径均不是账户PnL。"]
        if selected["selection_kind"] == "unresolved_observation":
            boundaries.insert(0, "本类型×环境整个已检查窗口没有已匹配证据；此案只观察未解决证据，不能称为该类龙股。")
        if identity in {"cross_regime_candidate", "sector_catchup_leader"}:
            boundaries.insert(0, "身份注册及跨环境证据受真实时钟限制，不能把15日登记回填成12—14日已知身份；旧行业分类不是当天题材。")
        if candidate is not None and candidate.get("ipo_feature_support") is False:
            boundaries.insert(0, "原候选明确IPO特征不支持；固定选样后保留此缺口，不补位、不升级资格。")
        if e["status"] != "observed_active_raw_bar":
            boundaries.insert(0, "固定选中后E日有效原始价格不足，案例保留，不以其他证券替代。")
        case = {"schema": CASE_SCHEMA, "method_id": METHOD_ID, "derived_at": recorded_at,
            "individual_case_interpretation_completed": False,
            **selected, "type_name": TYPE_NAMES[identity], "phase_name": PHASE_NAMES[selected["phase"]],
            "display_name": (candidate or {}).get("name") or feature.get("name") or symbol,
            "original_type_status": item["status"], "confirmed_leader": False,
            "environment_through_E": selection["environments"][day],
            "information_through_E": {"event_day": day,
                "saved_candidate": {k: deepcopy(candidate[k]) for k in CANDIDATE_FIELDS if k in candidate} if candidate else None,
                "saved_qualified_features": {k: _clean(feature[k]) for k in FEATURE_FIELDS if k in feature},
                "unknown_reason_for_symbol": unknown_reason,
                "unknown_reason_per_symbol_saved": unknown_reason is not None,
                "original_type_missing_evidence": deepcopy(item.get("missing_evidence")),
                "original_identity_proof": deepcopy(item.get("identity_proof")),
                "original_availability": deepcopy(item.get("availability")),
                "source_available_at": panel["available_at"],
                "field_date_cutoff_only_not_proven_historical_publication": True,
                "daily_slots": [r for r in slots if r["offset_sessions"] <= 0]},
            "after_E_observed_path": {"daily_slots": [r for r in slots if r["offset_sessions"] > 0],
                "future_coverage_used_to_refill_or_choose": False,
                "account_return": None, "strategy_success_or_failure": None},
            "daily_slots": slots,
            "counts": {"expected_calendar_slots": 5, "raw_rows": sum(r["raw"] is not None for r in slots),
                "missing_slots": sum(r["status"] == "missing_raw_row" for r in slots),
                "halted_slots": sum(r["status"] == "halted_source_row" for r in slots)},
            "fixed_research_conditions": deepcopy(TYPE_RULES[identity]),
            "question_to_test": QUESTIONS[identity], "limitations": boundaries, **FLAGS}
        cases.append(case)
    return {**selection, "cases": cases}


def _read_protocol(path, provenance, recorded_at):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("schema") != PROTOCOL_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("status") != "late_registered_retrospective_case_learning"
            or value.get("protocol") != PROTOCOL
            or Path(value.get("input_manifest", "")).resolve() != Path(provenance["panel"]).resolve()
            or value.get("input_manifest_sha256") != provenance["panel_sha256"]
            or not _stamp(provenance["created_at"]) <= _stamp(value["recorded_at"]) <= _stamp(recorded_at)):
        raise ValueError("Exact late-registered protocol, actual clocks and input hash required")
    for key in ("execution_admitted", "selection_enabled", "strategy_validated", "orders_generated"):
        if value.get(key) not in (None, False):
            raise ValueError("A case-learning protocol cannot admit execution or selection")
    return {"file": str(path), "sha256": digest(path), "recorded_at": value["recorded_at"],
        "status": value["status"], "protocol": deepcopy(PROTOCOL)}


def _show(value):
    return "待核验/缺失" if value is None or value == "" else str(value).replace("|", "／").replace("\n", " ")


def _markdown(case):
    info = case["information_through_E"]
    lines = [f"# {case['type_name']} · {case['phase_name']} · {case['display_name']} {case['symbol']}", "",
        f"E日：{case['day']}；选择性质：{case['selection_kind']}；原类型聚合状态：{case['original_type_status']}。",
        f"实际派生：{case['derived_at']}；原D面板实际可得：{info['source_available_at']}。", "",
        *["- " + text for text in case["limitations"]], "",
        "## E及以前的证据", "",
        "只按原类型×截面环境的日期、代码顺序取样；不以环境准入、成交额名次、后继价格或资格结果补位。",
        f"环境广度：{case['environment_through_E']['snapshot_inputs'].get('breadth')}；"
        f"当日截面中位涨跌幅：{case['environment_through_E']['snapshot_inputs'].get('median_return_pct')}%；"
        f"两收盘确认：{case['environment_through_E'].get('two_close_confirmation')}。",
        f"原类型缺证（聚合记录，不冒充逐股原因）：`{json.dumps(info['original_type_missing_evidence'], ensure_ascii=False)}`。",
        f"原身份资料：`{json.dumps(info['original_identity_proof'], ensure_ascii=False)}`。", "",
        "```json", json.dumps(info["saved_candidate"] if info["saved_candidate"] is not None else
            {"membership": "unresolved", "per_symbol_reason": info["unknown_reason_for_symbol"]}, ensure_ascii=False, indent=2), "```", "",
        "## 准确交易日路径", "",
        "E后行只作事后原始价格回顾。金额为元、量为股、提供者涨跌幅已是%；不换算为委托价、持仓或收益。", "",
        "| 相对E交易日 | 日期 | 区段/状态 | 开 | 高 | 低 | 收 | 提供者涨跌幅% | 成交额元 | 量股 | ST原值 | E后收盘低于E低点* |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|" ]
    for row in case["daily_slots"]:
        raw = row["raw"] or {}
        values = [row["offset_sessions"], row["day"], row["section"] + " / " + row["status"],
            *[raw.get(k) for k in ("open", "high", "low", "close", "pctChg", "amount", "volume", "isST")],
            row["close_below_fixed_E_raw_low"]]
        lines.append("| " + " | ".join(_show(v) for v in values) + " |")
    lines += ["", "*只在E到该日每个准确相邻参考链均满足原始前收差异≤1%的诊断代理时展示；不是行动调整、总收益或策略退出判定。",
        "", "## 下一条可检验问题", "", case["question_to_test"], "",
        "重叠案例：" + ("；".join(case["same_symbol_other_case_ids"]) or "本次未选中同证券的其他类型/环境案例") + "。同证券、同窗口和类型标签均可能重叠，不构成独立试验。", ""]
    return "\n".join(lines)


def build_case_studies(*, panel, ipo_evidence, research_master_run, protocol, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Type/environment cases require a new output directory")
    loaded = load_inputs(panel, ipo_evidence, research_master_run)
    recorded_at = datetime.now().astimezone().isoformat()
    registered = _read_protocol(protocol, loaded["provenance"], recorded_at)
    prepared = prepare_cases(loaded, recorded_at=recorded_at)
    original = loaded["provenance"]["input_record"]
    source = {k: deepcopy(v) for k, v in loaded["provenance"].items() if k != "input_record"}
    source.update({"identity_report": original.get("identity_report"), "industry_source": original.get("industry_source"),
        "turnover_sources": original.get("turnover_sources"), "side_sources": "stored manifest evidence; no new acquisition or side-source recomputation",
        "raw_window_source_days": loaded["source_days"], "context_anchor_not_read_for_case_bars": True})
    entries = []
    # Finish selection and all missing-path descriptions before writing anything.
    for case in prepared["cases"]:
        stem = "__".join((case["type_id"], case["phase"], case["day"].replace("-", ""), case["symbol"].replace(".", "_")))
        case.update(source=source, registered_case_protocol=registered, json_file=str(out / (stem + ".json")),
            markdown_file=str(out / (stem + ".md")))
    out.mkdir(parents=True, exist_ok=False)
    for case in prepared["cases"]:
        _write(Path(case["json_file"]), case)
        Path(case["markdown_file"]).write_text(_markdown(case), encoding="utf-8")
        entries.append({k: case[k] for k in ("case_id", "type_id", "phase", "day", "symbol", "display_name",
            "selection_kind", "selection_reason", "same_symbol_other_case_ids", "independent_sample", "counts", "json_file", "markdown_file")})
        entries[-1]["case_sha256"] = digest(Path(case["json_file"]))
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": recorded_at,
        "status": "completed_fixed_case_evidence_generation_not_deep_review",
        "individual_case_interpretation_completed": False,
        "protocol": registered, "source": source, "source_days": loaded["source_days"],
        "fixed_scope_member_count": len(loaded["scope_members"]), "coverage_matrix": prepared["coverage_matrix"],
        "environment_by_day": prepared["environments"], "cases": entries,
        "counts": {"cases": len(entries), "matched_proxy_cases": sum(r["selection_kind"] == "matched_proxy" for r in entries),
            "unresolved_observation_cases": sum(r["selection_kind"] == "unresolved_observation" for r in entries),
            "unique_symbols": len({r["symbol"] for r in entries}), "selected_cases_discarded_for_future_outcomes": 0,
            "raw_rows_in_case_windows_including_repeated_rows": sum(r["counts"]["raw_rows"] for r in entries),
            "missing_calendar_slots_in_case_windows": sum(r["counts"]["missing_slots"] for r in entries)},
        "sample_not_representative_of_all_leaders": True, "cases_and_labels_may_overlap": True, **FLAGS}
    _write(out / "manifest.json", result)
    lines = ["# 八类型×环境固定样本", "", "晚登记的事后逐案学习；不按赢家、后继完整性或收益选样，不是私人交割单或策略验证。",
        "本模块只自动整理事实与待验问题；不代表逐案深读已完成。", "",
        f"固定范围{result['fixed_scope_member_count']}证券；来源：{'、'.join(loaded['source_days'])}；案例{len(entries)}。",
        "矩阵中的数量为证券×日期观察数；同证券可跨环境和类型重复，不能相加作为独立样本数。", "",
        "| 类型 | 环境 | 已匹配 | 未解决 | 已知不匹配 | scope外 | 已选 | 覆盖 |",
        "|---|---|---:|---:|---:|---:|---:|---|"]
    for cell in result["coverage_matrix"]:
        lines.append("| " + " | ".join(str(v) for v in (cell["type_name"], PHASE_NAMES[cell["phase"]], cell["matched"],
            cell["unresolved"], cell["known_not_matched"], cell["outside_fixed_scope_count"], cell["selected"], cell["coverage_status"])) + " |")
    lines += ["", "## 固定顺序案例", ""] + [f"- [{r['type_id']} / {PHASE_NAMES[r['phase']]} / {r['day']} / {r['symbol']} · {r['selection_kind']}]({Path(r['markdown_file']).as_posix()})" for r in entries]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("panel", "ipo-evidence", "research-master-run", "protocol", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_case_studies(**vars(args))
    print(json.dumps({"status": result["status"], "out": str(args.out.resolve()), **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
