"""Bounded, reproducible public-disclosure case studies, never private blotters."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3

import pandas as pd
import pyarrow.parquet as pq

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .seats import derived_seat_entity, is_investor_category_aggregate


SCHEMA = "public_seat_stockday_case_learning_v1"
MANIFEST_SCHEMA = "public_seat_case_learning_catalog_v1"
FLAGS = {"personal_blotter_reconstructed": False, "person_identity_verified": False,
    "private_account_PnL_observable": False, "historical_publication_verified": False,
    "exchange_disclosure_completeness_verified": False, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "provider_contacted": False,
    "engine_run": False, "account_created": False, "source_modified": False}
PRICE_COLUMNS = ["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
    "adjustflag", "turn", "tradestatus", "pctChg", "isST"]


def disclosure_events(rows):
    """Merge identical event keys without summing buy/sell-board repeats."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["event_key"]].append(row)
    events = []
    for key, items in sorted(grouped.items()):
        first = items[0]
        event = {name: first[name] for name in ("trade_date", "symbol", "seat_name", "seat_code", "window_kind", "window_start", "window_end")}
        event.update(event_key=key, raw_listing_count=len(items), reasons=sorted({row["reason"] for row in items if row["reason"]}),
            listed_sides=sorted({row["listed_side"] for row in items if row["listed_side"]}),
            raw_ids=[row["raw_id"] for row in items], raw_files=sorted({row["raw_file"] for row in items}),
            local_first_retrieved_at=min(row["retrieved_at"] for row in items),
            published_at=sorted({row["published_at"] for row in items if row["published_at"]}) or None,
            identity_usable=all(row["identity_usable"] == 1 for row in items),
            amount_valid=all(row["amount_valid"] == 1 for row in items), amount_conflict=False,
            amount_aggregation="same_event_repeated_listings_not_summed", amount_observations={})
        event.update(derived_seat_entity(event["seat_name"], event["identity_usable"]))
        event["reported_identity_usable"] = event["identity_usable"]
        event["identity_usable"] = event["seat_identity_usable"]
        for field in ("buy_cny", "sell_cny", "net_cny", "period_turnover_cny"):
            known = sorted({row[field] for row in items if row[field] is not None})
            event[field] = known[0] if len(known) == 1 else None
            event["amount_observations"][field] = {"known_distinct_values": known,
                "missing_listing_count": sum(row[field] is None for row in items)}
            event["amount_conflict"] |= len(known) > 1
        events.append(event)
    versions = defaultdict(list)
    for event in events:
        comparable = event["seat_identity_usable"] and event["amount_valid"] and event["window_kind"] == "single"
        event["cross_event_amount_comparison"] = ("same_identifiable_single_day_seat" if comparable
            else "not_comparable_generic_identity_invalid_amount_or_non_single_window")
        if comparable:
            versions[(event["seat_name"], event["seat_code"], event["window_kind"], event["window_start"], event["window_end"])].append(event)
    for values in versions.values():
        signatures = {tuple(event[field] for field in ("buy_cny", "sell_cny", "net_cny", "period_turnover_cny")) for event in values}
        if len(signatures) > 1:
            for event in values:
                event["amount_conflict"] = True
                event["conflicting_event_keys"] = [other["event_key"] for other in values]
    return events


class PriceWindow:
    """Read only requested original daily row groups, with no next-row fallback."""
    def __init__(self, folder, sessions):
        self.folder = Path(folder).resolve()
        self.result_file = self.folder / "result.json"
        self.record = json.loads(self.result_file.read_text(encoding="utf-8"))
        if (self.record.get("status") != "source_cache_complete" or self.record.get("kind") != "stock"
                or self.record.get("execution_admitted") is not False):
            raise ValueError("Pinned complete raw stock source required; reconstructed levels are not raw prices")
        self.path = (self.folder / self.record["panel_file"]).resolve()
        self.path.relative_to(self.folder)
        self.calendar_file = self.folder / "input_calendar.csv"
        self.days = pd.read_csv(self.calendar_file, dtype=str).date.tolist()
        self.file = pq.ParquetFile(self.path)
        if (self.days != sorted(set(self.days)) or self.file.num_row_groups != len(self.days)
                or self.file.metadata.num_rows != self.record["rows"] or not set(PRICE_COLUMNS) <= set(self.file.schema_arrow.names)):
            raise ValueError("Exact raw date row groups and source shape required")
        self.index, self.sessions, self.cache = {day: i for i, day in enumerate(self.days)}, list(sessions), {}

    def row(self, day, symbol):
        if day not in self.index or not symbol.endswith((".SSE", ".SZSE")):
            return None
        if day not in self.cache:
            frame = self.file.read_row_group(self.index[day], columns=PRICE_COLUMNS).to_pandas()
            if not frame.date.eq(day).all() or frame.code.duplicated().any() or not frame.adjustflag.eq("3").all():
                raise ValueError("Raw daily group date/identity/price basis changed")
            self.cache[day] = frame.set_index("code", drop=False).to_dict("index")
        code = ("sh." if symbol.endswith(".SSE") else "sz.") + symbol[:6]
        row = self.cache[day].get(code)
        return dict(row) if row is not None else None

    def window(self, day, symbol):
        if day not in self.sessions:
            return {"status": "unknown_event_day_outside_pinned_calendar", "event_day": None, "before": [], "after": [], "exact_lags": {}}
        index = self.sessions.index(day)
        def observation(date):
            row = self.row(date, symbol)
            return {"day": date, "status": "observed_raw_daily_bar" if row else "unknown_exact_dated_bar_missing",
                "raw": row, "executable_price_verified": False}
        event = observation(day)
        lags = {}
        for lag in (1, 2, 5):
            exact = self.sessions[index + lag] if index + lag < len(self.sessions) else None
            row = self.row(exact, symbol) if exact else None
            before = _positive((event["raw"] or {}).get("close"))
            close = _positive((row or {}).get("close"))
            lags[str(lag)] = {"required_day": exact, "raw": row,
                "unadjusted_close_change_pct": (close / before - 1) * 100 if before and close else None,
                "missing_exact_bar": row is None, "next_available_substituted": False,
                "economic_total_return_verified": False, "personal_PnL": None}
        return {"status": "observed_raw_context_not_execution_or_total_return", "event_day": event,
            "before": [observation(date) for date in self.sessions[max(0, index - 5):index]],
            "after": [observation(date) for date in self.sessions[index + 1:index + 6]], "exact_lags": lags,
            "window_rule": "exact_five_pinned_sessions_before_and_after; event_close_and_forward_context_separate",
            "future_bars_are_retrospective_context_only": True, "company_actions_verified": False}

    def proof(self):
        return {"folder": str(self.folder), "raw_file": str(self.path), "result_file": str(self.result_file),
            "result_sha256": digest(self.result_file), "expected_full_panel_sha256": self.record.get("panel_sha256"),
            "full_panel_hash_recomputed": False, "original_source_rows": self.file.metadata.num_rows,
            "original_source_row_groups": self.file.num_row_groups, "source_calendar": str(self.calendar_file),
            "source_calendar_sha256": digest(self.calendar_file), "read_row_groups": len(self.cache),
            "price_basis": "provider_raw_adjustflag_3", "independent_prices_verified": False}


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _markdown(case):
    lines = [f"# {case['day']} {case['symbol']} 公开席位事实复盘", "",
        "自动事实提取，尚未逐案人工深读；不是个人交割单或个人盈亏。", "",
        f"股票汇总理由条数：{len(case['stock_disclosures'])}；原席位列示：{len(case['raw_seat_observations'])}；去重复列示事件：{len(case['seat_events'])}。", "",
        "| 窗口 | 原名称 | 派生实体类型 | 已披露买入元 | 已披露卖出元 | 金额冲突 |", "|---|---|---|---:|---:|---|"]
    for event in case["seat_events"]:
        values = [event["window_kind"], event["seat_name"], event.get("entity_kind", "未单独分类"),
                  event["buy_cny"], event["sell_cny"], event["amount_conflict"]]
        lines.append("| " + " | ".join("未知" if value is None else str(value).replace("|", "/") for value in values) + " |")
    lines += ["", "单日与多日窗口分别保留；同一事件的买榜/卖榜重复列示不累计。多日窗口起点未知时保持未知。匿名泛称或非单日窗口不按同一主体跨事件比较金额。investor_category_aggregate为可能重叠的投资者统计类别，不能相加或视为营业部/个人；原标记保留于reported_identity_usable。", "",
        "| 准确后继交易日间隔 | 必需日期 | 原始收盘价变化%（非收益/盈亏） |", "|---|---|---:|"]
    for lag, value in case["price_window"]["exact_lags"].items():
        change = value["unadjusted_close_change_pct"]
        lines.append(f"| {lag} | {value['required_day'] or '未知'} | {'未知' if change is None else f'{change:.4f}'} |")
    lines += ["", "事件日前后日线完整原值、每条榜单理由和原件路径见同名 JSON。后继价格只作事后背景；历史披露时间未知，不能作为当日盘前信号。", "",
        "人物关联只收原登记的精确席位名；本批媒体关系全部未证实，简称与名称变更不自动匹配。", "", "待人工核对：", ""]
    lines += ["- " + item for item in case["review_questions"]]
    return "\n".join(lines) + "\n"


def _batch_catalog(catalog, offset, limit):
    if offset < 0 or offset >= len(catalog) or limit < 1:
        raise ValueError("Fixed batch offset must identify an existing catalog position")
    selected = catalog[offset:offset + limit]
    for index, item in enumerate(catalog):
        item.update(case_id=item["day"].replace("-", "") + "_" + item["symbol"],
            status="planned_fixed_prefix" if offset <= index < offset + len(selected) else
                "outside_this_batch_prior_prefix_not_reprocessed" if index < offset else "pending_not_processed")
    return selected


def correct_saved_cases(manifest_path, out):
    """Correct only derived event labels from saved facts; no database/prices."""
    manifest_path, out = Path(manifest_path).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Correction needs a new output; preserve the original evidence")
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    if original.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("Original public-seat case manifest required")
    loaded = []
    for item in original["cases"]:
        path = Path(item["json_file"])
        if digest(path) != item["json_sha256"]:
            raise ValueError("Original saved fact case changed")
        loaded.append((item, path, json.loads(path.read_text(encoding="utf-8"))))
    out.mkdir(parents=True, exist_ok=False)
    outputs = []
    for item, path, case in loaded:
        events = disclosure_events(case["raw_seat_observations"])
        case.update(seat_events=events,
            windows={kind: [row["event_key"] for row in events if row["window_kind"] == kind]
                for kind in sorted({row["window_kind"] for row in events}, key=str)},
            derived_at=datetime.now().astimezone().isoformat(), fact_extraction_revision=2,
            derived_from_case={"file": str(path), "sha256": item["json_sha256"]},
            correction="cross_event_amount_conflict_only_for_identity_usable_amount_valid_single_day_seats",
            original_raw_disclosures_and_prices_reused_unchanged=True)
        json_file, markdown_file = out / (case["case_id"] + ".json"), out / (case["case_id"] + ".md")
        write_json(json_file, case)
        markdown_file.write_text(_markdown(case), encoding="utf-8")
        outputs.append({**item, "json_file": str(json_file), "markdown_file": str(markdown_file),
            "json_sha256": digest(json_file), "derived_from_case": case["derived_from_case"]})
    corrected = {**original, "created_at": datetime.now().astimezone().isoformat(), "cases": outputs,
        "fact_extraction_revision": 2, "derived_from_manifest": {"file": str(manifest_path), "sha256": digest(manifest_path)},
        "original_catalog_reused": True, "database_or_price_source_reopened": False,
        "correction": "Anonymous/generic and non-single windows are not cross-event amount conflicts; all original rows and price windows preserved"}
    write_json(out / "manifest.json", corrected)
    return corrected


def run(database, price_source, calendar, out, *, start="2026-09-01", end="2026-09-24", limit=100, offset=0):
    if start > end or limit < 1 or offset < 0:
        raise ValueError("Ordered explicit interval and a positive fixed prefix limit required")
    out, database, calendar = map(lambda value: Path(value).resolve(), (out, database, calendar))
    if out.exists():
        raise FileExistsError("Case studies require a new output folder")
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar" or start not in sessions or end not in sessions:
        raise ValueError("Pinned exact-session calendar must cover both requested bounds")
    prices = PriceWindow(price_source, sessions)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        catalog = [dict(row) for row in db.execute("""SELECT trade_date day,symbol,
            GROUP_CONCAT(DISTINCT stock_name) source_stock_names,COUNT(*) summary_rows
            FROM a_share_stock_disclosures WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date,symbol ORDER BY trade_date,symbol""", (start, end))]
        links = [dict(row) for row in db.execute("SELECT * FROM person_seat_links ORDER BY link_id")]
        private_count = db.execute("SELECT COUNT(*) FROM private_trade_documents").fetchone()[0]
        selected, outputs = _batch_catalog(catalog, offset, limit), []
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "person_seat_links.json", {"rows": links, "row_count": len(links),
            "identity_verified_count": sum(row["identity_verified"] == 1 for row in links),
            "automatic_abbreviation_or_historical_alias_mapping": False, **FLAGS})
        for item in selected:
            day, symbol = item["day"], item["symbol"]
            summaries = [dict(row) for row in db.execute("SELECT * FROM stock_disclosures WHERE trade_date=? AND symbol=? ORDER BY raw_id", (day, symbol))]
            original = [dict(row) for row in db.execute("SELECT * FROM seat_observations WHERE trade_date=? AND symbol=? ORDER BY raw_id", (day, symbol))]
            events = disclosure_events(original)
            names = {row["seat_name"] for row in original if not is_investor_category_aggregate(row["seat_name"])}
            linked = [row for row in links if row["exact_seat_name"] and row["exact_seat_name"] in names]
            case = {"schema": SCHEMA, "case_id": item["case_id"], "day": day, "symbol": symbol,
                "derived_at": datetime.now().astimezone().isoformat(), "stock_disclosures": summaries,
                "raw_seat_observations": original, "seat_events": events,
                "windows": {kind: [row["event_key"] for row in events if row["window_kind"] == kind]
                    for kind in sorted({row["window_kind"] for row in events}, key=str)},
                "exact_name_media_attributions": linked, "media_identity_or_validity_inferred": False,
                "private_trade_documents_in_dataset": private_count, "price_window": prices.window(day, symbol),
                "review_status": "automatic_factual_extraction_pending_human_case_review", "fact_extraction_revision": 2,
                "learning_hypotheses": ["公开披露窗口与交易触发结构的关系需要逐案检验；当前没有个人动机或胜率结论。"],
                "review_questions": ["每条异常交易理由对应单日还是多日窗口？起点是否确知？",
                    "同额买榜与卖榜列示是否为同一披露事件？单边缺失与金额冲突是否已保留？",
                    "事件前形态及后继准确交易日价格如何变化？涨跌停、公司行动与可成交性有哪些未证据？",
                    "人物归属是否有本人或交易所证据？当前媒体简称和未知有效期不能补成个人账户身份。",
                    "哪些预先可观察条件值得作为独立假设，哪些解释依赖事后价格？"],
                "sources": {"database": str(database), "provider": "Eastmoney republication of public disclosures",
                    "provider_url": "https://datacenter-web.eastmoney.com/api/data/v1/get",
                    "official_exchange_originals_independently_verified": False,
                    "price_source": str(prices.path), "calendar": str(calendar)}, **FLAGS}
            json_file, markdown_file = out / (item["case_id"] + ".json"), out / (item["case_id"] + ".md")
            write_json(json_file, case)
            markdown_file.write_text(_markdown(case), encoding="utf-8")
            item["status"] = "facts_extracted_pending_manual_review"
            outputs.append({"case_id": item["case_id"], "symbol": symbol, "day": day,
                "json_file": str(json_file), "markdown_file": str(markdown_file), "json_sha256": digest(json_file)})
        catalog_file = out / "catalog.csv"
        pd.DataFrame(catalog).to_csv(catalog_file, index=False, encoding="utf-8-sig")
        dataset_status = database.parent / "status.json"
        manifest = {"schema": MANIFEST_SCHEMA, "created_at": datetime.now().astimezone().isoformat(),
            "status": "completed_bounded_public_fact_extraction_not_manual_deep_review",
            "start": start, "end": end, "catalog_count": len(catalog), "processed_count": len(outputs),
            "pending_count": max(0, len(catalog) - offset - len(outputs)), "manual_deep_review_completed": 0,
            "skipped_prefix_count": offset, "not_extracted_in_this_manifest_count": len(catalog) - len(outputs),
            "pending_count_semantics": "unprocessed_catalog_suffix_after_this_batch; prior_prefix_status_not_asserted_by_this_manifest",
            "catalog_file": str(catalog_file), "catalog_sha256": digest(catalog_file), "cases": outputs,
            "sample_rule": "all_A_share_stockdays_in_explicit_interval; date_then_symbol_ascending; fixed_catalog_slice[offset:offset+limit]; no_return_person_or_success_filter",
            "fixed_prefix_limit": limit, "fixed_offset": offset, "prior_prefix_reprocessed": False,
            "fact_extraction_revision": 2, "person_seat_link_count": len(links), "private_trade_documents": private_count,
            "source_refs": {"database": str(database), "database_mode": "SQLite mode=ro plus query_only",
                "dataset_status_file": str(dataset_status), "dataset_status_sha256": digest(dataset_status) if dataset_status.is_file() else None,
                "price": prices.proof(), "calendar": str(calendar), "calendar_sha256": digest(calendar)},
            "boundaries": ["Supplier pagination coverage is not independent exchange completeness or complete personal trades.",
                "Raw listings and distinct events are not numbers of executions; repeated sides are never added together.",
                "Multi-day and unknown windows remain separate; an unknown start is never reconstructed from an assumed three days.",
                "Missing amounts remain null; conflicting event versions are preserved, not maximized or silently reconciled.",
                "Displayed stock names and media attributions are not verified historical identity or person/account ownership.",
                "Raw close changes are retrospective unadjusted context, not executable returns, total returns or personal PnL.",
                "Facts and initial review questions were generated automatically; no claim that a human read every original is made."], **FLAGS}
        write_json(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("database", "price-source", "calendar"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--correct-from-manifest", type=Path)
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-09-24")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    if args.correct_from_manifest is not None:
        result = correct_saved_cases(args.correct_from_manifest, args.out)
    else:
        if not all((args.database, args.price_source, args.calendar)):
            parser.error("--database, --price-source and --calendar are required for a new batch")
        result = run(args.database, args.price_source, args.calendar, args.out, start=args.start, end=args.end, limit=args.limit, offset=args.offset)
    print(json.dumps({key: result[key] for key in ("status", "catalog_count", "processed_count", "pending_count", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
