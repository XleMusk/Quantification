"""Join explicit case studies and public-source learning cards, without trading."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

LOCAL = timezone(timedelta(hours=8))
FLAGS = {"execution_admitted": False, "selection_enabled": False,
         "strategy_validated": False, "orders_generated": False,
         "ledger_mutated": False, "ths_payload_generated": False}
EVIDENCE_LABELS = {
    "official_identity_only": "平台身份资料，尚无成交记录",
    "first_person_intention_with_unread_image": "本人表达，附图交易字段未读",
    "first_person_execution_self_report": "本人自报操作，券商成交未独立核实",
    "official_identified_trade_facts": "官方查明的局部个人交易",
    "official_exchange_field_definition": "交易所披露字段说明",
    "primary_conversation_author_attribution_audit": "逐回复作者核对",
    "legacy_reference_not_newly_full_read": "旧索引，原文待核",
    "cross_platform_post_summary_unbound": "跨平台方法线索，未绑定同花顺原帖",
    "first_person_risk_self_report": "本人自报亏损与仓位，尚无券商成交核实",
    "first_person_method_self_report": "本人方法讨论，未披露具体成交",
}
NOTES = {
    "beijing_20200717_risk_self_report": (
        "原作者自述参与太平洋并报告损失和约八分之一仓位，构成亏损学习线索。",
        "缺成交单和完整资金，不能倒推买价、股数、账户总额或精确回撤。先问这笔风险为什么值得承担、何时决定退出。"),
    "beijing_20230306_position_method": (
        "本人讨论集中仓位与波动的关系，并尝试增加分仓数量；这不是已证实的固定回撤上限。",
        "可研究持仓分散，但数量需结合相关性、费用、资金和100股约束另检验，不能直接把帖子里的数量改成策略参数。"),
    "ths_yeren_official_identity_reused": (
        "已找对同花顺账号，但身份资料不提供买卖、价格、数量或账户收益。",
        "原生具体操作帖尚未取得；暂时不能逐笔还原其交易。"),
    "duan_20261005_primary_discussion": (
        "能确认的是本人谈到卖出put；不能从评论区补出合约、数量或成交价。",
        "学习时把公司判断、建仓工具和实际成交分开。期权操作也不能直接变成A股接力规则。"),
    "ordinary_investor_20260411_self_report": (
        "原作者自报已卖50张，另外50张是有条件的计划，不能记成第二笔成交。",
        "缺标的、成交价、保证金、账户模式和后续平仓，不能据此算收益或仿造订单。"),
    "szse_identified_person_trade_control_example": (
        "官方文件给出了局部买卖日期、股数和披露价格。它是风险案例，不是游资成功策略。",
        "两笔股数差不能当剩余持仓；四舍五入均价不能还原逐笔成交，缺费用也不能算个人净收益。"),
    "szse_2026_public_trading_disclosure_definition": (
        "龙虎榜披露营业部或交易单元的金额，同一席位可以包含多个客户。",
        "单日与异常期间累计金额分别保留；未上榜不代表没交易，净买额也不能解释为个人加仓。"),
    "duan_reply_author_boundary_verified": (
        "这页关于现金流的长段来自另一位回复者，不能全部归到段永平名下。",
        "逐条记录正文及回复的实际作者，只保留已核实的本人观点。原归因不能继续作为该观点的出处。"),
}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def _link(path):
    return Path(path).resolve().as_posix()


def _public_cards(index, yeren_seed, folder):
    rows = list(index["sources"]) + list(index["reused_duan_six_source_inventory"])
    for number, event in enumerate(yeren_seed["events"], 1):
        rows.append({"id": f"yeren_cross_platform_method_{number:02d}",
            "url": event["source"], "author": "跨平台公开索引；身份关联未逐帖证实",
            "evidence_level": "cross_platform_post_summary_unbound",
            "source_published_at": {"date_or_range": event["date"],
                                    "precision": "date_only_as_prior_index"},
            "facts": {"prior_summary": event["post_summary"],
                      "rule_hypothesis": event["rule_hypothesis"],
                      "required_observations": event["observability"]},
            "trade_fields": {key: None for key in ("symbol", "side", "qty", "price")},
            "unknowns": ["同花顺原帖及作者逐帖绑定", "具体成交与完整账户"],
            "full_original_text_read_this_turn": False})
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Public source case IDs must be unique")
    cards = []
    for row in rows:
        identity = row["id"]
        if not identity or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in identity):
            raise ValueError("Unexpected public source ID")
        level = row["evidence_level"]
        label = EVIDENCE_LABELS[level]
        notes = NOTES.get(identity)
        if notes is None:
            notes = (("这条资料仍是研究线索，尚未完成原文逐条核对。",
                      "先核作者、时间和完整上下文，不从摘要推断买卖或仓位。")
                     if level == "legacy_reference_not_newly_full_read" else
                     ("公开摘要提供方法假设，不能当成已核实的同花顺本人交割单。",
                      "按当时市场、入场条件、仓位变化和退出条件补齐数据后，另做独立区间检验。"))
        case = {"schema": "public_source_learning_card_v1", "case_id": identity,
                "source_level": level, "source_level_label": label,
                "source_record": row, "learning_notes": list(notes),
                "strategy_parameters_changed": False,
                "complete_personal_blotter": False, **FLAGS}
        json_file, md_file = folder / f"{identity}.json", folder / f"{identity}.md"
        _write(json_file, case)
        body = [f"# {identity}", "", label + "。", "",
                f"原始资料：[来源]({row['url']})。作者：{row.get('author') or '待逐条核实'}。", "",
                "记录的时间精度：" + json.dumps(row.get("source_published_at"), ensure_ascii=False), "",
                "## 本案学什么", "", *[f"- {note}" for note in notes], "",
                "## 已记录的事实与未知", "", "```json",
                json.dumps({key: row.get(key) for key in
                    ("facts", "trade_fields", "unknowns", "read_status")}, ensure_ascii=False, indent=2),
                "```", "", "本卡不代表完整账户记录，也不生成交易参数或委托。", ""]
        md_file.write_text("\n".join(body), encoding="utf-8")
        cards.append({"case_id": identity, "kind": "public_source",
                      "source_level": level, "symbol": None,
                      "day": row.get("date") or row.get("date_as_prior_index"),
                      "json_file": _link(json_file), "markdown_file": _link(md_file)})
    return cards


def _person_catalog(dataset, out):
    dataset = Path(dataset).resolve()
    with closing(sqlite3.connect(f"file:{(dataset / 'disclosures.sqlite3').as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        links = [dict(row) for row in db.execute("SELECT * FROM person_seat_links ORDER BY person_label,link_id")]
        private_count = db.execute("SELECT COUNT(*) FROM private_trade_documents").fetchone()[0]
    groups = {}
    for row in links:
        groups.setdefault(row["person_label"], []).append(row)
    people = [{"person_label": label, "original_links": rows,
               "individual_trade_history_established_by_links": False,
               "learning_status": "needs_attributable_original_transactions",
               "notes": "原媒体席位关联逐条保留，不把营业部交易归给个人，不倒推过去或现在账户。"}
              for label, rows in groups.items()]
    value = {"schema": "public_trader_learning_inventory_v1", "people": people,
             "person_labels": len(people), "original_link_rows": len(links),
             "private_trade_documents_in_source": private_count,
             "source_database": _link(dataset / "disclosures.sqlite3"), **FLAGS}
    _write(out, value)
    return value


def build(leader_manifest, seat_manifest, public_index, yeren_seed, dataset, out, seat_review=None):
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("Use a new casebook output directory; previous learning records are preserved")
    seat_paths = seat_manifest if isinstance(seat_manifest, (tuple, list)) else [seat_manifest]
    seat_paths = [Path(value).resolve() for value in seat_paths]
    leader_manifest, public_index, yeren_seed = map(
        lambda value: Path(value).resolve(), (leader_manifest, public_index, yeren_seed))
    leader, public, seed = map(_read, (leader_manifest, public_index, yeren_seed))
    seats = [_read(path) for path in seat_paths]
    scopes = {(row["start"], row["end"], row["catalog_count"]) for row in seats}
    if len(scopes) != 1:
        raise ValueError("This casebook joins batches from one explicit dated disclosure scope")
    records = []
    for kind, document in [("leader_price_path", leader), *[("seat_disclosure", item) for item in seats]]:
        for row in document["cases"]:
            item = {key: row[key] for key in ("case_id", "symbol", "day", "json_file", "markdown_file")}
            for field in ("json_file", "markdown_file"):
                if not Path(item[field]).is_file():
                    raise ValueError(f"Missing actual case artifact: {item[field]}")
                item[field] = _link(item[field])
            records.append({**item, "kind": kind})
    seat_ids = [row["case_id"] for row in records if row["kind"] == "seat_disclosure"]
    if len(seat_ids) != len(set(seat_ids)) or len(seat_ids) > seats[0]["catalog_count"]:
        raise ValueError("Seat case batches overlap or exceed their fixed scope")
    if public.get("schema") != "public_trade_case_primary_source_index_v1":
        raise ValueError("Expected the explicitly checked public-source index")
    reviews = []
    interpretation_files = {}
    review_paths = ([] if seat_review is None else
                    [Path(item).resolve() for item in (seat_review if isinstance(seat_review, (list, tuple)) else [seat_review])])
    for review_path in review_paths:
        review_doc = _read(review_path)
        cases_by_id = {row["case_id"]: row for row in records if row["kind"] == "seat_disclosure"}
        for row in review_doc["reviews"]:
            case = cases_by_id.get(row["case_id"])
            if (case is None or Path(row["case_file"]).resolve() != Path(case["json_file"]).resolve()
                    or sha256(Path(row["case_file"]).read_bytes()).hexdigest() != row["case_sha256"]):
                raise ValueError("Individual interpretation must refer to the exact current fact case")
            reviews.append(row["case_id"])
            interpretation_files[row["case_id"]] = _link(review_path)
        if len(reviews) != len(set(reviews)):
            raise ValueError("Repeated interpretations do not count as different cases")
    ordered_seats = sorted((row for row in records if row["kind"] == "seat_disclosure"),
                           key=lambda row: (row["day"], row["symbol"], row["case_id"]))
    pending_reviews = []
    reviewed_prefix = 0
    for position, row in enumerate(ordered_seats, 1):
        row["individual_interpretation_file"] = interpretation_files.get(row["case_id"])
        row["individual_interpretation_status"] = "agent_interpreted" if row["case_id"] in interpretation_files else "pending"
        if row["case_id"] not in interpretation_files:
            pending_reviews.append({"catalog_position": position, **row})
        elif not pending_reviews:
            reviewed_prefix += 1
    out.mkdir(parents=True)
    queue = {"schema": "seat_case_fixed_order_unread_queue_v1",
             "order": "event_day_symbol_case_id_ascending_without_outcome_filter",
             "facts_extracted": len(ordered_seats), "individual_interpretations_completed": len(reviews),
             "consecutive_reviewed_prefix": reviewed_prefix,
             "pending_individual_interpretations": len(pending_reviews), "cases": pending_reviews,
             "outcomes_used_to_order": False, **FLAGS}
    _write(out / "unread_review_queue.json", queue)
    public_folder = out / "public_cases"
    public_folder.mkdir()
    records += _public_cards(public, seed, public_folder)
    persons = _person_catalog(dataset, out / "trader_inventory.json")
    manifest = {"schema": "individual_case_learning_book_v1",
        "created_at": datetime.now(LOCAL).isoformat(),
        "source_files": [{"path": _link(path), "sha256": sha256(path.read_bytes()).hexdigest()}
                         for path in (leader_manifest, *seat_paths, public_index, yeren_seed,
                                      *review_paths)],
        "counts": dict(Counter(row["kind"] for row in records)),
        "public_source_levels": dict(Counter(row.get("source_level") for row in records if row["kind"] == "public_source")),
        "trader_inventory": _link(out / "trader_inventory.json"),
        "unread_review_queue": _link(out / "unread_review_queue.json"),
        "seat_scope_counts": {"start": seats[0]["start"], "end": seats[0]["end"],
            "catalog_total": seats[0]["catalog_count"], "facts_extracted": len(seat_ids),
            "pending_extraction": seats[0]["catalog_count"] - len(seat_ids),
            "individual_interpretations_completed": len(reviews),
            "pending_individual_interpretations": len(seat_ids) - len(reviews),
            "manual_deep_review_completed": 0}, "leader_sample_rule": leader.get("sample_rule"),
        "cases": records, "case_cards_are_not_complete_manual_reviews": True,
        "new_strategy_backtest_run": False, "strategy_parameters_changed": False, **FLAGS}
    _write(out / "manifest.json", manifest)
    counts = manifest["counts"]
    lines = ["# 龙头与公开交易逐案学习", "",
        f"已生成{counts.get('leader_price_path', 0)}个个股日线路径复盘、{counts.get('seat_disclosure', 0)}个席位股票日事实卡、{counts.get('public_source', 0)}条公开来源学习卡。", "",
        f"人物目录保留原{persons['person_labels']}个媒体称呼、{persons['original_link_rows']}条席位关联，尚不能据此确认个人交易。源库私人交割文档数为{persons['private_trade_documents_in_source']}。", "",
        "个股样本按固定日期、代码规则选择，没有按后续涨幅筛选；本批使用事后取得的历史资料。席位批次按日期、代码排序，包含成功、失败和缺失资料。事实卡完成不等于已经深读全部交易，更不代表复现了个人收益。", "",
        f"公开来源卡分别是{len(public['sources'])}条核查来源（含既有身份资料复用）、{len(public['reused_duan_six_source_inventory'])}条旧参考待核、{len(seed['events'])}条跨平台方法线索。它们不是完整个人交割单。", "",
        f"席位范围{manifest['seat_scope_counts']['catalog_total']}个股票日，已提取{len(seat_ids)}案、待提取{manifest['seat_scope_counts']['pending_extraction']}案；已逐案解读{len(reviews)}案、待解读{len(seat_ids)-len(reviews)}案。不能把自动整理的数量报成学习完成数量。", "",
        f"[剩余逐案清单]({_link(out / 'unread_review_queue.json')})按事件日、代码固定排序，连续已读前缀为{reviewed_prefix}案；每张事实卡分别记录解读文件或待读状态。", "",
        "## 每案要回答的事", "",
        "1. 原始资料是谁发布的，什么时候能看到，属于本人交割、自述还是席位汇总？",
        "2. 当时市场、行业与个股是什么状态；哪些是当时信息，哪些是后续结果？",
        "3. 买卖、加减仓和不交易的理由能否从原文证明；价格、数量、费用缺什么？",
        "4. 原来的判断何时失效，有没有因涨停、跌停或T+1无法按计划退出？",
        "5. 哪部分能写成独立检验的规则，哪部分仍是盘感或事后解释？", "",
        "## 已查明的一处归因问题", "",
        "旧段永平讨论页索引把一段现金流观点宽泛归给页头作者；本次逐回复核对发现长段作者另有其人。新学习卡已收窄归因，原始档案保留。未读截图与评论区推测也没有补成成交字段。", "",
        "## 先读已完成的学习笔记", ""]
    notes = public_index.parent / "FIRST_CASE_LESSONS.md"
    if notes.is_file():
        lines.append(f"- [首批五个个股的具体教训]({_link(notes)})")
    for review_path in review_paths:
        lines.append(f"- [席位逐案解读：{review_path.parent.name}]({_link(review_path.parent / 'REVIEW.md')})")
    post_notes = public_index.parent / "yeren_cross_platform_13" / "REVIEW.md"
    if post_notes.is_file():
        lines.append(f"- [含证券代码的13条跨平台帖文学习]({_link(post_notes)})（尚未逐帖绑定同花顺账号）")
    lines += ["", "## 逐案入口", "", "|类别|日期|证券或来源ID|复盘|", "|---|---|---|---|"]
    labels = {"leader_price_path": "个股路径", "seat_disclosure": "席位披露", "public_source": "公开资料"}
    for row in records:
        lines.append(f"|{labels[row['kind']]}|{row.get('day') or '日期未明'}|{row.get('symbol') or row['case_id']}|[打开]({row['markdown_file']})|")
    lines += ["", "下一批沿原日期代码顺序读取剩余案例，补本人原始成交资料；新假设另找独立时间段检验，不修改已经看过结果的固定规则。", "",
              "所有卡片只用于研究，未生成账户交易或同花顺预填。", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("leader-manifest", "public-index", "yeren-seed", "dataset", "out"):
        parser.add_argument("--" + field, required=True, type=Path)
    parser.add_argument("--seat-manifest", required=True, type=Path, action="append")
    parser.add_argument("--seat-review", type=Path, action="append")
    args = parser.parse_args(argv)
    result = build(args.leader_manifest, args.seat_manifest, args.public_index, args.yeren_seed, args.dataset, args.out,
                   seat_review=args.seat_review)
    print(json.dumps({"out": str(args.out.resolve()), "counts": result["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
