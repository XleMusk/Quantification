"""Local dated case learning; no strategy replay or personal-trade attribution.

The original corrected annotation's future run labels and research_eligible
are deliberately never read. Case selection uses one fixed session's fields;
later daily paths remain a separate retrospective evidence section.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view


SCHEMA = "leader_dated_daily_lifecycle_case_v1"
METHOD_ID = "fixed_session_stage2_symbol_first5_case_learning_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "orders_generated": False,
    "ledger_mutated": False, "provider_contacted": False, "personal_blotter_reproduced": False,
    "historical_signal_publication_verified": False, "corporate_actions_verified": False, "pit_verified": False}
RAW_FIELDS = ["code", "date", "open", "high", "low", "close", "volume", "amount",
              "pct_chg", "is_st", "pre_close", "turnover"]
MARKET_FIELDS = ["market_row_count", "market_up_count", "market_down_count", "market_limitup_count", "market_limitdown_count"]
EVENT_FIELDS = ["event_id", "symbol", "date", "board", "stage", "is_st", "open", "high", "low", "close",
    "amount", "volume", "pct_chg_source", "limit_up", "price_reference_status", *MARKET_FIELDS]
SELECTION = {"id": METHOD_ID, "fixed_event_stage": 2, "minimum_amount_cny": 20_000_000,
    "order": "canonical_symbol_ascending_first5", "status": "late_registered_retrospective_case_learning",
    "reference": "event_date_only_match_within_1pct", "ST": "event_date_false",
    "future_fields_not_used": ["research_eligible", "run_price_reference_status", "run_end_date",
        "run_max_stage_observed", "hit_4", "hit_5", "future_return", "future_coverage"],
    "failed_or_incomplete_cases_retained": True, "sample_not_representative_of_all_leaders": True}


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if value is pd.NA or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _clean(value.item())
    return value


def select_samples(events, event_day):
    """Ignore future outcomes, even if a caller includes old annotation columns."""
    candidates = events.loc[events.date.eq(event_day) & events.stage.eq(2)
        & events.is_st.eq(False) & events.amount.ge(20_000_000)
        & events.price_reference_status.eq("match_within_1pct")
        & events.board.eq("main")].sort_values("symbol", kind="stable")
    if candidates.symbol.duplicated().any():
        raise ValueError("A dated case sample needs one event per symbol")
    return candidates.loc[:, EVENT_FIELDS].head(5).to_dict("records")


def _limit_proxy(row):
    """Original corrected event main-board rounding; no IPO/PIT promotion."""
    try:
        close, pct, volume = (float(row[key]) for key in ("close", "pct_chg", "volume"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (close, pct, volume)) or close <= 0 or pct <= -100 or volume <= 0:
        return None
    reference = close / (1 + pct / 100)
    return math.floor(close * 100 + .5) >= math.floor(reference * 1.1 * 100 + .5)


def summarize_case(rows, event, expected_days):
    """A close proxy breaking is distinct from a falling close or a trade loss."""
    day = event["date"]
    observed = {row["date"]: row for row in rows}
    missing = [value for value in expected_days if value not in observed]
    setup_low = float(event["low"])
    later, first_break, chain_unknown = [], None, False
    continuation = []
    for value in expected_days:
        if value <= day:
            continue
        row = observed.get(value)
        if row is None:
            chain_unknown = True
            later.append({"date": value, "status": "missing_raw_observation", "suspension_inferred": False,
                "below_fixed_event_low": None})
            continue
        flag = _limit_proxy(row)
        if first_break is None and not chain_unknown:
            if flag is True:
                continuation.append(value)
            elif flag is False:
                first_break = value
            else:
                chain_unknown = True
        later.append({**row, "close_limit_proxy": flag,
            "below_fixed_event_low": float(row["close"]) < setup_low,
            "daily_range_percent_of_reconstructed_preclose":
                (float(row["high"]) - float(row["low"])) / (float(row["close"]) / (1 + float(row["pct_chg"]) / 100)) * 100})
    first_observed_breach = next((row["date"] for row in later if row.get("below_fixed_event_low") is True), None)
    comparison_prefix = [row for row in later if first_observed_breach is None or row["date"] < first_observed_breach]
    comparison_unknown = not later or any(row.get("below_fixed_event_low") is None for row in comparison_prefix)
    breach_status = "unknown" if comparison_unknown else ("known_first" if first_observed_breach else "complete_none")
    negative = [row["date"] for row in later if row.get("pct_chg") is not None and float(row["pct_chg"]) < 0]
    return {"fixed_event_setup_low": setup_low, "consecutive_limit_closes_after_E_until_first_break": continuation,
        "first_observed_close_limit_break_day": first_break,
        "first_break_closes_price_down": (float(observed[first_break]["pct_chg"]) < 0) if first_break else None,
        "first_observed_close_below_fixed_event_low": first_observed_breach,
        "first_close_below_fixed_event_low": first_observed_breach if not comparison_unknown else None,
        "first_close_below_fixed_event_low_status": breach_status,
        "negative_close_days_after_E": negative, "exact_expected_missing_raw_days": missing,
        "missing_row_is_not_suspension_or_zero_return": True,
        "later_daily_path": later, "original_qualified_limit_or_structure_exit_admitted": False}


def _write(path, value):
    path.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _markdown(case):
    event, path = case["information_through_E"], case["after_E_observed_path"]
    row = event["selected_event"]
    if path["first_close_below_fixed_event_low"]:
        breach_description = f"准确首次收盘跌破为 **{path['first_close_below_fixed_event_low']}**。"
    elif path.get("first_close_below_fixed_event_low_status") == "complete_none":
        breach_description = "E后准确比较窗完整，窗口内未见收盘跌破。"
    elif path.get("first_observed_close_below_fixed_event_low"):
        breach_description = (f"首次已观察到收盘跌破为 **{path['first_observed_close_below_fixed_event_low']}**；"
            "此前比较证据不完整，准确首次收盘跌破日 **未知**。")
    else:
        breach_description = "准确首次收盘跌破日 **未知**，没有足够完整的E后比较证据，不能将缺槽当作未跌破。"
    lines = [f"# {case['display_name']}（{case['symbol']}）日期样本复盘", "",
        f"固定入选日 **{case['event_day']}**；研究展示窗 {case['window_start']}—{case['window_end']}。"
        "该日原二板事件按代码顺序前五，不按后来成败或四五板标签选样；属于事后资料学习，不是当时发布的推荐或个人交割单。", "",
        f"截至E的字段：原日线二板代理，收盘{row['close']:.2f}元、成交额{row['amount']/1e8:.4f}亿元；"
        f"当日参考价状态{row['price_reference_status']}。"
        "这些字段只描述截至该日的内容，不证明当时系统已取得这些资料；原标签未获逐日IPO/PIT全验证。", "",
        f"E后事实：首个连续收盘涨停代理中断日 **{path['first_observed_close_limit_break_day'] or '未知'}**，"
        f"中断日是否收跌：{path['first_break_closes_price_down']}。"
        f"固定E日低点{path['fixed_event_setup_low']:.2f}元，{breach_description}"
        "断板、负涨跌幅、跌破预设结构是三件不同的事，均不等于账户亏损。", "",
        "| 日期 | 开/高/低/收（元） | 原pct% | 成交额（亿元） | 原turnover字段% | 原事件板次 |", "|---|---|---:|---:|---:|---:|"]
    for daily in case["daily_rows"]:
        lines.append(f"| {daily['date']} | {daily['open']:.2f}/{daily['high']:.2f}/{daily['low']:.2f}/{daily['close']:.2f} | "
            f"{daily['pct_chg']:.3f} | {daily['amount']/1e8:.4f} | {daily['turnover']} | {daily.get('original_event_stage') or '—'} |")
    lines += ["", "可证阶段：", "",
        *[f"- {item}" for item in case["phase_evidence"]], "",
        "可检验假设：在完整、事前固定的二板事件母集中，分别检验断板、E日低点失守与环境广度变化的后继可成交风险。"
        "本案仅供提出问题，不能据此回调止损、挑选赢家或声称已找到有效战法。", "",
        "仍缺：分钟/逐笔与封单顺序、真实买卖成交及T+1可卖量、个人账户归属、当时题材/PIT与公司行动全覆盖；"
        "日线振幅不能还原先涨后跌、炸板回封、盘口承接或净流入。原turnover仅保留来源百分数字段，没有独立流通股数证明。", "",
        "原事件表仅保留一至三板事件，表中板次“—”不表示没有涨停；四板以上须另看逐日原价代理。"
        "证券简称来自2026-10-14名册，仅用于展示，不证明历史名称或历史ST资格。", "",
        f"缺失的2026准确交易日：{', '.join(path['exact_expected_missing_raw_days']) or '无'}；"
        "2025左侧上下文只有原观察行，未额外声称已核2025完整交易日历。", "",
        f"资料实际派生时间：{case['derived_at']}；源快照记录创建时间：{case['source']['snapshot_recorded_created_at']}。"
        "源注释表没有精确发布时钟，文件修改时间仅作文件系统旁证，不能当历史公开时间。", "",
        f"[逐案JSON]({Path(case['json_file']).as_posix()}) · [原事件资料]({Path(case['source']['annotations_file']).as_posix()}) · "
        f"[源manifest]({Path(case['source']['snapshot_manifest']).as_posix()})", "",
        "`strategy_validated=false`；`selection_enabled=false`；`execution_admitted=false`。"]
    return "\n".join(lines) + "\n"


def build_case_studies(*, annotations, snapshot, calendar, names_roster, out,
                      event_day="2026-01-05", window_start="2025-12-29", window_end="2026-01-16"):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Case learning must use a new output directory")
    annotations, snapshot, calendar, names_roster = map(lambda value: Path(value).resolve(),
        (annotations, snapshot, calendar, names_roster))
    events = pd.read_parquet(annotations, columns=EVENT_FIELDS)
    samples = select_samples(events, event_day)
    if not samples:
        raise ValueError("No samples under the fixed date-only selection")
    symbols = [row["symbol"] for row in samples]
    codes = {symbol[:6] for symbol in symbols}
    prefixes = {symbol[0] for symbol in symbols}
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    sessions, _, status = _calendar_view(payload, str(payload["year"]) + "-01-01", str(payload["year"]) + "-12-31", [])
    if status != "pinned_static_exchange_calendar" or event_day not in sessions:
        raise ValueError("Fixed event session must be in the pinned calendar")
    expected = [day for day in sessions if event_day <= day <= window_end]
    parts, proofs = [], []
    low, high = int(window_start.replace("-", "")), int(window_end.replace("-", ""))
    for item in manifest["chunks"]:
        if (item["prefix"] not in prefixes or int(item["upper_inclusive"]) < low or int(item["lower_inclusive"]) > high):
            continue
        file = snapshot / item["parquet_file"]
        if digest(file) != item["parquet_sha256"]:
            raise ValueError("Pinned original case partition changed")
        frame = pd.read_parquet(file, columns=RAW_FIELDS)
        if len(frame) != item["rows"]:
            raise ValueError("Pinned original case partition row count changed")
        parts.append(frame.loc[frame.code.isin(codes) & frame.date.between(low, high)].copy())
        proofs.append({"file": str(file), "sha256": item["parquet_sha256"], "source_rows": item["rows"]})
    raw = pd.concat(parts, ignore_index=True)
    raw["date"] = pd.to_datetime(raw.date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    if raw.duplicated(["code", "date"]).any():
        raise ValueError("Case raw observations duplicate a dated code")
    for event in samples:
        physical = raw.loc[raw.code.eq(event["symbol"][:6]) & raw.date.eq(event_day)]
        if len(physical) != 1 or any(float(physical.iloc[0][key]) != float(event[key]) for key in
                                    ("open", "high", "low", "close", "volume", "amount")):
            raise ValueError("Selected E event does not exactly match its saved raw observation")
    names = pd.read_csv(names_roster, dtype=str).set_index("symbol").code_name.to_dict()
    selected_events = events.loc[events.symbol.isin(symbols) & events.date.between(window_start, window_end)]
    event_stages = {(row["symbol"], row["date"]): int(row["stage"]) for row in selected_events.to_dict("records")}
    market = events.loc[events.date.between(window_start, window_end), ["date", *MARKET_FIELDS]].drop_duplicates()
    if market.date.duplicated().any():
        raise ValueError("Same-date original provider market counts disagree")
    market_by_day = market.set_index("date").to_dict("index")
    derived = datetime.now().astimezone().isoformat()
    out.mkdir(parents=True, exist_ok=False)
    source = {"annotations_file": str(annotations), "annotations_sha256": digest(annotations),
        "annotation_recorded_publication_time": None,
        "annotations_filesystem_mtime_not_publication": datetime.fromtimestamp(annotations.stat().st_mtime).astimezone().isoformat(),
        "snapshot_manifest": str(snapshot / "manifest.json"), "snapshot_manifest_sha256": digest(snapshot / "manifest.json"),
        "snapshot_recorded_created_at": manifest["created_at"], "partitions": proofs,
        "calendar_file": str(calendar), "calendar_sha256": digest(calendar),
        "name_roster_file": str(names_roster), "name_roster_sha256": digest(names_roster),
        "display_name_date": "2026-10-14", "display_name_is_not_historical_name_or_ST_qualification": True,
        "raw_preclose_retained": True, "daily_limit_reference": "separate_pct_reconstructed_legacy_diagnostic_only"}
    case_index = []
    for event in samples:
        symbol = event["symbol"]
        daily = raw.loc[raw.code.eq(symbol[:6])].sort_values("date").to_dict("records")
        for row in daily:
            row["symbol"] = symbol
            row["original_event_stage"] = event_stages.get((symbol, row["date"]))
            row["stage_missing_does_not_mean_no_limit"] = row["original_event_stage"] is None
        path = summarize_case(daily, event, expected)
        startup = [row["date"] for row in daily if row["date"] <= event_day and row["original_event_stage"] == 1]
        phases = ["启动：" + ("原事件表首板代理日期" + startup[-1] if startup else "左侧资料不足，保留未知") + "，不等于已确认题材龙头。",
            "分歧：逐日日线给出最高/最低/收盘与成交额，可描述宽幅或收盘未封板；盘内先后次序、换手承接未知。",
            "连续封板：E后二板链延续收盘日期" + ("、".join(path["consecutive_limit_closes_after_E_until_first_break"]) or "无已确认延续日") + "；日线不能证明盘口加速。",
            "退潮/修复：展示所有后继正负收盘及固定E低点是否失守，不把断板自动当退潮或删掉后续修复；缺失日期不当停牌。"]
        json_file = out / (symbol.replace(".", "_") + ".json")
        case = {"schema": SCHEMA, "case_id": symbol + "|" + event_day, "symbol": symbol,
            "display_name": names.get(symbol, symbol), "event_day": event_day, "window_start": window_start,
            "window_end": window_end, "derived_at": derived, "selection": SELECTION,
            "source": source, "information_through_E": {"selected_event": event,
                "earlier_observed_daily_rows": [row for row in daily if row["date"] <= event_day],
                "market_count_proxy_at_E": market_by_day.get(event_day),
                "field_date_cutoff_only_not_proven_historical_availability": True},
            "after_E_observed_path": path, "daily_rows": daily, "phase_evidence": phases,
            "market_counts_by_day": market_by_day, "json_file": str(json_file),
            "hypothesis_not_result": "fixed_stage2_cohort_close_break_or_frozen_setup_break_vs_next_session_fillability_and_market_breadth",
            "full_lifecycle_or_personal_trade_claimed": False, **FLAGS}
        _write(json_file, case)
        markdown = json_file.with_suffix(".md")
        markdown.write_text(_markdown(case), encoding="utf-8")
        case_index.append({"symbol": symbol, "display_name": case["display_name"], "json": str(json_file),
            "markdown": str(markdown), "first_break": path["first_observed_close_limit_break_day"],
            "first_fixed_setup_breach": path["first_close_below_fixed_event_low"],
            "missing_expected_days": path["exact_expected_missing_raw_days"]})
    result = {"schema": "leader_dated_case_learning_index_v1", "method_id": METHOD_ID, "derived_at": derived,
        "event_day": event_day, "selection": SELECTION, "cases": case_index, "source": source, **FLAGS}
    _write(out / "index.json", result)
    _write(out / "manifest.json", {"schema": "leader_case_study_manifest_v1", "derived_at": derived,
        "method_id": METHOD_ID, "sample_rule": {"event_day": event_day, **SELECTION}, "source": source,
        "counts": {"cases": len(case_index), "selected_cases_discarded_for_future_outcomes": 0},
        "cases": [{"case_id": row["symbol"] + "|" + event_day, "symbol": row["symbol"], "day": event_day,
            "json_file": row["json"], "markdown_file": row["markdown"]} for row in case_index], **FLAGS})
    (out / "README.md").write_text("# 固定日期首批五案\n\n" + "不按后续赢家选样，所有五案及缺失均保留；原未来标签未读取，不是私人交割单或策略验证。\n\n" +
        "\n".join(f"- [{row['display_name']} {row['symbol']}]({Path(row['markdown']).as_posix()})" for row in case_index) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("annotations", "snapshot", "calendar", "names-roster", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--event-day", default="2026-01-05")
    parser.add_argument("--window-start", default="2025-12-29")
    parser.add_argument("--window-end", default="2026-01-16")
    args = parser.parse_args(argv)
    result = build_case_studies(**vars(args))
    print(json.dumps({"case_count": len(result["cases"]), "out": str(Path(args.out).resolve()), **FLAGS}, ensure_ascii=False))


if __name__ == "__main__":
    main()
