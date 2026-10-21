"""Command-line interface for the A-share committee system."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .engine import (
    build_ai_prompt,
    create_empty_thesis,
    create_sample_thesis,
    default_review_path,
    generate_review_markdown,
    load_thesis,
    save_thesis,
)
from .alpha_stack import (
    alpha_markdown,
    alpha_feedback_markdown,
    alpha_outcome_markdown,
    append_alpha_log,
    etf_snapshot_cache_status,
    evaluate_alpha_log,
    fetch_etf_snapshots,
    rank_event_reactions,
    save_alpha_report,
    save_alpha_outcome_report,
    save_alpha_feedback_report,
    score_manual_event,
    summarize_alpha_feedback,
)
from .catalyst_monitor import NewsItem, catalyst_markdown, fetch_realtime_news, save_catalyst_report, scan_catalysts
from .event_radar import build_event_radar, event_radar_markdown, save_event_radar_report
from .intraday_breadth import classify_intraday_breadth, intraday_breadth_markdown, save_intraday_breadth_report
from .market_regime import (
    classify_market_regime,
    load_latest_market_regime,
    market_regime_markdown,
    save_market_regime_report,
)
from .position_sizing import (
    calculate_a_share_position,
    result_to_markdown,
    save_position_report,
)
from .risk_radar import (
    fetch_symbol_notice_items,
    fetch_symbol_risk_items,
    risk_markdown,
    save_risk_report,
    scan_risk_items,
    symbol_risk_flags,
)
from .market_data import cache_summary, ensure_daily_data, normalize_symbol
from .radar_config import ensure_default_config, load_config, save_config, small_account_config
from .screeners import generate_stock_report, save_screen_result, screen_symbols, stock_report_for_symbol
from .skill_catalog import catalog_markdown, skill_detail_markdown
from .trend_calibration import (
    DEFAULT_TARGETS,
    KNOWN_TARGETS,
    backfill_trend_history,
    default_history_start,
    evaluate_targets,
    market_trend_markdown,
    save_market_trend_review,
    select_targets,
    trend_history_cache_markdown,
    trend_history_cache_summary,
)
from .trader_memory import close_thesis, format_thesis_table, list_theses, register_thesis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_committee.py",
        description="A股大师投委会学习系统：创建研究模板、生成审查报告和AI导师提示词。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="创建一份空白研究模板")
    init_parser.add_argument("--out", default="committee_journal/new_thesis.json")
    init_parser.add_argument("--stock-name", default="")
    init_parser.add_argument("--code", default="")

    sample_parser = subparsers.add_parser("sample", help="创建一份示例研究模板")
    sample_parser.add_argument("--out", default="committee_journal/sample_thesis.json")

    review_parser = subparsers.add_parser("review", help="生成投委会审查报告")
    review_parser.add_argument("input", help="研究模板JSON路径")
    review_parser.add_argument("--out", default="")
    review_parser.add_argument("--print", action="store_true", dest="print_report")

    prompt_parser = subparsers.add_parser("prompt", help="生成可复制给AI导师的提示词")
    prompt_parser.add_argument("input", help="研究模板JSON路径")
    prompt_parser.add_argument("--out", default="")
    prompt_parser.add_argument("--print", action="store_true", dest="print_prompt")

    skills_parser = subparsers.add_parser("skills", help="列出本地A股skills目录")
    skills_parser.add_argument("--out", default="")

    skill_info_parser = subparsers.add_parser("skill-info", help="查看某个skill详情")
    skill_info_parser.add_argument("name")
    skill_info_parser.add_argument("--out", default="")

    size_parser = subparsers.add_parser("size", help="A股仓位计算")
    size_parser.add_argument("--account-equity", type=float, required=True)
    size_parser.add_argument("--entry", type=float, required=True)
    size_parser.add_argument("--stop", type=float, default=None)
    size_parser.add_argument("--stop-pct", type=float, default=None)
    size_parser.add_argument("--risk-pct", type=float, default=0.5)
    size_parser.add_argument("--max-position-pct", type=float, default=5)
    size_parser.add_argument("--output-dir", default="committee_reports")
    size_parser.add_argument("--print", action="store_true", dest="print_size")

    memory_add_parser = subparsers.add_parser("memory-add", help="登记一份研究模板到交易记忆")
    memory_add_parser.add_argument("input")
    memory_add_parser.add_argument("--state-dir", default="committee_state/theses")

    memory_list_parser = subparsers.add_parser("memory-list", help="列出交易记忆")
    memory_list_parser.add_argument("--state-dir", default="committee_state/theses")
    memory_list_parser.add_argument("--status", default="")
    memory_list_parser.add_argument("--code", default="")

    memory_close_parser = subparsers.add_parser("memory-close", help="关闭一条交易记忆")
    memory_close_parser.add_argument("thesis_id")
    memory_close_parser.add_argument("--exit-price", type=float, required=True)
    memory_close_parser.add_argument("--reason", required=True)
    memory_close_parser.add_argument("--lesson", action="append", default=[])
    memory_close_parser.add_argument("--state-dir", default="committee_state/theses")

    fetch_parser = subparsers.add_parser("fetch-stock", help="抓取并缓存一只A股日线数据")
    fetch_parser.add_argument("symbol")
    fetch_parser.add_argument("--start", default="")
    fetch_parser.add_argument("--end", default="")
    fetch_parser.add_argument("--force", action="store_true")

    cache_parser = subparsers.add_parser("data-cache", help="查看本地A股数据缓存")
    cache_parser.add_argument("--print", action="store_true", dest="print_cache")

    spot_cache_parser = subparsers.add_parser("spot-cache", help="查看或预热ETF实时快照缓存")
    spot_cache_parser.add_argument("--refresh", action="store_true", help="强制刷新ETF实时快照缓存")

    breadth_parser = subparsers.add_parser("intraday-breadth", help="用ETF实时快照判断盘中弱修复/强修复")
    breadth_parser.add_argument("--cache-seconds", type=int, default=120, help="ETF实时快照缓存秒数，默认120秒")
    breadth_parser.add_argument("--force-spot", action="store_true", help="忽略缓存并强制刷新ETF快照")
    breadth_parser.add_argument("--output-dir", default="committee_reviews")
    breadth_parser.add_argument("--print", action="store_true", dest="print_intraday_breadth")

    report_parser = subparsers.add_parser("stock-report", help="生成一只A股决策支持报告")
    report_parser.add_argument("symbol")
    report_parser.add_argument("--start", default="")
    report_parser.add_argument("--end", default="")
    report_parser.add_argument("--force", action="store_true")
    report_parser.add_argument("--risk-gate", action="store_true", help="先扫描个股公告/新闻风险，红灯则并入BLOCK")
    report_parser.add_argument("--risk-notice-days", type=int, default=120, help="风险门禁扫描公告天数")
    report_parser.add_argument("--print", action="store_true", dest="print_stock_report")

    screen_parser = subparsers.add_parser("screen", help="对一组A股做基础筛选")
    screen_parser.add_argument("--symbols", default="", help="逗号分隔，如 600000.SSE,000001.SZSE")
    screen_parser.add_argument("--file", default="", help="一行一个股票代码")
    screen_parser.add_argument("--start", default="")
    screen_parser.add_argument("--end", default="")
    screen_parser.add_argument("--strategy", default="all", choices=["all", "canslim-lite", "vcp-lite", "value-lite", "trend-watch"])
    screen_parser.add_argument("--top", type=int, default=30)
    screen_parser.add_argument("--force", action="store_true")
    screen_parser.add_argument("--config", default="")
    screen_parser.add_argument("--risk-gate", action="store_true", help="先扫描个股公告/新闻风险，红灯则并入BLOCK")
    screen_parser.add_argument("--risk-notice-days", type=int, default=120, help="风险门禁扫描公告天数")

    radar_config_parser = subparsers.add_parser("radar-config", help="创建或查看候选雷达配置")
    radar_config_parser.add_argument("--account-equity", type=float, default=10_000)
    radar_config_parser.add_argument("--out", default="")
    radar_config_parser.add_argument("--print", action="store_true", dest="print_radar_config")

    trend_parser = subparsers.add_parser("market-trend", help="核验并校准近期A股宽基/科技板块趋势")
    trend_parser.add_argument("--start", default="", help="留空则按 --years 回溯")
    trend_parser.add_argument("--end", default="")
    trend_parser.add_argument("--years", type=int, default=5, help="默认回溯近5年")
    trend_parser.add_argument("--evaluate-date", default="", help="用上一交易日预测该日期并核验，如 2026-05-21")
    trend_parser.add_argument("--lookback", type=int, default=160)
    trend_parser.add_argument("--targets", default="", help="逗号分隔的 target_id 或代码；默认覆盖宽基和科技ETF代理")
    trend_parser.add_argument("--event", default="", choices=["", "nvl72_bom"], help="显式事件覆盖层，如 nvl72_bom")
    trend_parser.add_argument("--force-history", action="store_true", help="强制刷新趋势历史缓存")
    trend_parser.add_argument("--force-params", action="store_true", help="强制重新校准趋势参数")
    trend_parser.add_argument("--no-param-cache", action="store_true", help="不使用趋势参数缓存")
    trend_parser.add_argument("--no-cache", action="store_true", help="不使用本地趋势历史缓存")
    trend_parser.add_argument("--output-dir", default="committee_reviews")
    trend_parser.add_argument("--print", action="store_true", dest="print_market_trend")

    regime_parser = subparsers.add_parser("market-regime", help="判定当前A股是弱修复、强修复还是退潮防守")
    regime_parser.add_argument("--start", default="", help="留空则按 --years 回溯")
    regime_parser.add_argument("--end", default="")
    regime_parser.add_argument("--years", type=int, default=5, help="默认回溯近5年")
    regime_parser.add_argument("--lookback", type=int, default=160)
    regime_parser.add_argument("--event", default="", choices=["", "nvl72_bom"], help="显式事件覆盖层，如 nvl72_bom")
    regime_parser.add_argument("--force-history", action="store_true", help="强制刷新趋势历史缓存")
    regime_parser.add_argument("--force-params", action="store_true", help="强制重新校准趋势参数")
    regime_parser.add_argument("--no-param-cache", action="store_true", help="不使用趋势参数缓存")
    regime_parser.add_argument("--no-cache", action="store_true", help="不使用本地趋势历史缓存")
    regime_parser.add_argument("--output-dir", default="committee_reviews")
    regime_parser.add_argument("--print", action="store_true", dest="print_market_regime")

    history_parser = subparsers.add_parser("trend-history-cache", help="抓取/查看近几年宽基和科技ETF趋势历史缓存")
    history_parser.add_argument("--years", type=int, default=5, help="默认抓取近5年")
    history_parser.add_argument("--start", default="", help="指定起始日期会覆盖 --years")
    history_parser.add_argument("--end", default="")
    history_parser.add_argument("--targets", default="", help="逗号分隔的 target_id 或代码；默认覆盖宽基和科技ETF代理")
    history_parser.add_argument("--refresh", action="store_true", help="刷新/补齐历史缓存")
    history_parser.add_argument("--force", action="store_true", help="强制重新抓取并覆盖缓存")
    history_parser.add_argument("--print", action="store_true", dest="print_history_cache")

    catalyst_parser = subparsers.add_parser("catalyst-scan", help="扫描新闻/研报催化剂并映射A股主题代理")
    catalyst_parser.add_argument("--limit", type=int, default=80)
    catalyst_parser.add_argument("--threshold", type=float, default=7.0)
    catalyst_parser.add_argument("--text", default="", help="直接扫描一段研报/新闻文本")
    catalyst_parser.add_argument("--file", default="", help="扫描本地文本文件")
    catalyst_parser.add_argument("--inbox-dir", default="", help="扫描目录内的 .txt/.md 研报文本")
    catalyst_parser.add_argument("--output-dir", default="committee_reviews")
    catalyst_parser.add_argument("--print", action="store_true", dest="print_catalysts")

    risk_parser = subparsers.add_parser("risk-radar", help="扫描A股暴雷/退市/ST/监管/非标审计等风险")
    risk_parser.add_argument("--limit", type=int, default=120)
    risk_parser.add_argument("--threshold", type=float, default=5.0)
    risk_parser.add_argument("--text", default="", help="直接扫描一段公告/新闻/研报文本")
    risk_parser.add_argument("--file", default="", help="扫描本地文本文件")
    risk_parser.add_argument("--inbox-dir", default="", help="扫描目录内的 .txt/.md 风险线索")
    risk_parser.add_argument("--symbols", default="", help="逗号分隔个股代码，如 002342,600745")
    risk_parser.add_argument("--symbol-file", default="", help="一行一个个股代码，用于扫描持仓/自选股风险")
    risk_parser.add_argument("--notice-days", type=int, default=120, help="扫描个股公告的最近天数")
    risk_parser.add_argument("--red-only", action="store_true", help="只输出红灯风险")
    risk_parser.add_argument("--new-event-only", action="store_true", help="隐藏仅由ST简称等触发的存量风险状态")
    risk_parser.add_argument("--output-dir", default="committee_reviews")
    risk_parser.add_argument("--print", action="store_true", dest="print_risk")

    event_radar_parser = subparsers.add_parser("event-radar", help="扫描催化剂并自动接入ETF事件Alpha排序")
    event_radar_parser.add_argument("--limit", type=int, default=80)
    event_radar_parser.add_argument("--threshold", type=float, default=7.0)
    event_radar_parser.add_argument("--top-events", type=int, default=3, help="参与Alpha排序的最高分催化剂数量")
    event_radar_parser.add_argument("--text", default="", help="直接扫描一段研报/新闻文本")
    event_radar_parser.add_argument("--file", default="", help="扫描本地文本文件")
    event_radar_parser.add_argument("--inbox-dir", default="", help="扫描目录内的 .txt/.md 研报文本")
    event_radar_parser.add_argument("--cache-seconds", type=int, default=120, help="ETF实时快照缓存秒数，默认120秒")
    event_radar_parser.add_argument("--force-spot", action="store_true", help="忽略缓存并强制刷新ETF快照")
    event_radar_parser.add_argument("--ignore-regime", action="store_true", help="不读取最新市场环境闸门")
    event_radar_parser.add_argument("--no-log", action="store_true", help="不追加事件信号日志")
    event_radar_parser.add_argument("--output-dir", default="committee_reviews")
    event_radar_parser.add_argument("--print", action="store_true", dest="print_event_radar")

    alpha_parser = subparsers.add_parser("event-alpha", help="按催化剂+实时市场反应+拥挤度给ETF代理排序")
    alpha_parser.add_argument("--text", required=True, help="研报/新闻催化剂文本")
    alpha_parser.add_argument("--threshold", type=float, default=7.0)
    alpha_parser.add_argument("--cache-seconds", type=int, default=120, help="ETF实时快照缓存秒数，默认120秒")
    alpha_parser.add_argument("--force-spot", action="store_true", help="忽略缓存并强制刷新ETF快照")
    alpha_parser.add_argument("--no-log", action="store_true", help="不追加事件信号日志")
    alpha_parser.add_argument("--output-dir", default="committee_reviews")
    alpha_parser.add_argument("--print", action="store_true", dest="print_alpha")

    alpha_eval_parser = subparsers.add_parser("event-alpha-evaluate", help="评估已记录事件信号的后续表现")
    alpha_eval_parser.add_argument("--log", default="committee_state/signals/event_alpha_log.jsonl")
    alpha_eval_parser.add_argument("--limit", type=int, default=5, help="评估最近N条信号记录，0表示全部")
    alpha_eval_parser.add_argument("--cache-seconds", type=int, default=120, help="ETF实时快照缓存秒数，默认120秒")
    alpha_eval_parser.add_argument("--force-spot", action="store_true", help="忽略缓存并强制刷新ETF快照")
    alpha_eval_parser.add_argument("--output-dir", default="committee_reviews")
    alpha_eval_parser.add_argument("--print", action="store_true", dest="print_alpha_eval")

    alpha_feedback_parser = subparsers.add_parser("event-alpha-feedback", help="按历史事件信号结果生成参数反馈")
    alpha_feedback_parser.add_argument("--log", default="committee_state/signals/event_alpha_log.jsonl")
    alpha_feedback_parser.add_argument("--limit", type=int, default=0, help="评估最近N条信号记录，0表示全部")
    alpha_feedback_parser.add_argument("--min-samples", type=int, default=5)
    alpha_feedback_parser.add_argument("--cache-seconds", type=int, default=120, help="ETF实时快照缓存秒数，默认120秒")
    alpha_feedback_parser.add_argument("--force-spot", action="store_true", help="忽略缓存并强制刷新ETF快照")
    alpha_feedback_parser.add_argument("--output-dir", default="committee_reviews")
    alpha_feedback_parser.add_argument("--print", action="store_true", dest="print_alpha_feedback")

    inventory_parser = subparsers.add_parser("quant-inventory", help="Inspect local quantitative research assets (offline)")
    inventory_parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parents[1] / "outputs/quant_foundation/inventory"))
    research_parser = subparsers.add_parser("quant-research", help="Run historical factor diagnostics and optional native Qlib baseline")
    research_parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "committee_config/quant_foundation.json"))
    research_parser.add_argument("--output-dir", default=None, help="New directory; existing runs are never overwritten")
    research_parser.add_argument("--backtest", action="store_true", help="Include historical execution-proxy backtest; no live orders")
    research_parser.add_argument("--allow-partial-backtest", action="store_true", help="Run a quality-gated partial backtest after excluding symbols with invalid OHLC or missing feature files; never promotes or enables live execution")

    args = parser.parse_args(argv)

    if args.command == "quant-inventory":
        from .quant_inventory import save_inventory
        result = save_inventory(args.output_dir)
        print(f"Inventory: {Path(args.output_dir).resolve() / 'INVENTORY.md'}")
        print(f"Local caches: {len(result['daily_caches'])}; live_ready=False")
        return 0

    if args.command == "quant-research":
        from .quant_foundation import run_research
        out, result = run_research(args.config, args.output_dir, args.backtest, args.allow_partial_backtest)
        print(f"Report: {out.resolve() / 'REPORT.md'}")
        print(f"Status: {result['status']}; strategy_validated=False; live_ready=False")
        return 2 if result["status"] == "partial_backtest_blocked" else 0

    if args.command == "init":
        thesis = create_empty_thesis()
        thesis["stock_name"] = args.stock_name
        thesis["code"] = args.code
        path = save_thesis(thesis, args.out)
        print(f"已创建研究模板: {path}")
        return 0

    if args.command == "sample":
        path = save_thesis(create_sample_thesis(), args.out)
        print(f"已创建示例模板: {path}")
        return 0

    if args.command == "review":
        thesis = load_thesis(args.input)
        report = generate_review_markdown(thesis)
        out = Path(args.out) if args.out else default_review_path(thesis)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"已生成审查报告: {out}")
        if args.print_report:
            print()
            print(report)
        return 0

    if args.command == "prompt":
        thesis = load_thesis(args.input)
        prompt = build_ai_prompt(thesis)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(prompt, encoding="utf-8")
            print(f"已生成AI导师提示词: {out}")
        if args.print_prompt or not args.out:
            print(prompt)
        return 0

    if args.command == "skills":
        text = catalog_markdown()
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"已导出skills目录: {out}")
        else:
            print(text)
        return 0

    if args.command == "skill-info":
        text = skill_detail_markdown(args.name)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"已导出skill详情: {out}")
        else:
            print(text)
        return 0

    if args.command == "size":
        result = calculate_a_share_position(
            account_equity=args.account_equity,
            entry_price=args.entry,
            stop_price=args.stop,
            stop_pct=args.stop_pct,
            risk_pct=args.risk_pct,
            max_position_pct=args.max_position_pct,
        )
        json_path, md_path = save_position_report(result, args.output_dir)
        print(f"已生成仓位报告: {md_path}")
        print(f"已生成仓位JSON: {json_path}")
        if args.print_size:
            print()
            print(result_to_markdown(result))
        return 0

    if args.command == "memory-add":
        thesis = load_thesis(args.input)
        item = register_thesis(thesis, state_dir=args.state_dir, source_path=args.input)
        print(f"已登记交易想法: {item['thesis_id']} ({item['status']})")
        return 0

    if args.command == "memory-list":
        items = list_theses(
            state_dir=args.state_dir,
            status=args.status or None,
            code=args.code or None,
        )
        print(format_thesis_table(items))
        return 0

    if args.command == "memory-close":
        item = close_thesis(
            args.thesis_id,
            exit_price=args.exit_price,
            reason=args.reason,
            state_dir=args.state_dir,
            lessons=args.lesson,
        )
        print(f"已关闭交易想法: {item['thesis_id']} ({item['status']})")
        return 0

    if args.command == "fetch-stock":
        df = ensure_daily_data(
            args.symbol,
            start_date=args.start or None,
            end_date=args.end or None,
            force=args.force,
        )
        print(
            "已缓存 {symbol}: {rows} 行，{start} ~ {end}".format(
                symbol=df["vt_symbol"].iloc[0],
                rows=len(df),
                start=df["date"].min().strftime("%Y-%m-%d"),
                end=df["date"].max().strftime("%Y-%m-%d"),
            )
        )
        return 0

    if args.command == "data-cache":
        summary = cache_summary()
        if summary.empty:
            print("暂无本地A股缓存。")
        elif args.print_cache:
            print(summary.to_string(index=False))
        else:
            print(
                "本地缓存: {n} 个文件，约 {size:.2f} MB".format(
                    n=len(summary),
                    size=summary["size_mb"].sum(),
                )
            )
        return 0

    if args.command == "spot-cache":
        if args.refresh:
            snapshots = fetch_etf_snapshots(KNOWN_TARGETS, cache_seconds=0, force=True)
            print(f"已刷新ETF实时快照缓存: {len(snapshots)} 个标的")
        status = etf_snapshot_cache_status()
        print(f"缓存文件: {status['path']}")
        print(f"存在: {status['exists']}")
        print(f"保存时间: {status['saved_at']}")
        print(f"缓存年龄秒: {status['age_seconds']}")
        print(f"标的数量: {status['count']}")
        if status["error"]:
            print(f"错误: {status['error']}")
        return 0

    if args.command == "intraday-breadth":
        snapshots = fetch_etf_snapshots(
            KNOWN_TARGETS,
            cache_seconds=args.cache_seconds,
            force=args.force_spot,
        )
        breadth = classify_intraday_breadth(snapshots)
        csv_path, md_path = save_intraday_breadth_report(breadth, snapshots, output_dir=args.output_dir)
        print(f"已生成盘中修复强度报告: {md_path}")
        print(f"CSV: {csv_path}")
        print(f"结论: {breadth.label} | 风险闸门: {breadth.risk_gate}")
        if args.print_intraday_breadth:
            print()
            print(intraday_breadth_markdown(breadth, snapshots))
        return 0

    if args.command == "stock-report":
        external_flags = (
            _build_external_risk_flags([args.symbol], days=args.risk_notice_days)
            if args.risk_gate
            else None
        )
        decision, path = stock_report_for_symbol(
            args.symbol,
            start_date=args.start or None,
            end_date=args.end or None,
            force=args.force,
            external_risk_flags=external_flags,
        )
        print(f"已生成决策支持报告: {path}")
        print(f"{decision.vt_symbol} {decision.latest_date} score={decision.score:.1f} label={decision.label}")
        if external_flags:
            print(f"风险门禁BLOCK: {sum(len(items) for items in external_flags.values())} 条")
        if args.print_stock_report:
            print()
            print(generate_stock_report(decision))
        return 0

    if args.command == "screen":
        symbols = _load_symbols(args.symbols, args.file)
        if not symbols:
            parser.error("screen requires --symbols or --file")
        config = load_config(args.config) if args.config else load_config(ensure_default_config())
        external_flags = (
            _build_external_risk_flags(symbols, days=args.risk_notice_days)
            if args.risk_gate
            else None
        )
        df = screen_symbols(
            symbols,
            start_date=args.start or None,
            end_date=args.end or None,
            strategy=args.strategy,
            top=args.top,
            force=args.force,
            config=config,
            external_risk_flags=external_flags,
        )
        csv_path, md_path = save_screen_result(df)
        print(f"已生成筛选结果: {md_path}")
        print(f"CSV: {csv_path}")
        if external_flags:
            print(f"风险门禁BLOCK: {sum(len(items) for items in external_flags.values())} 条")
        if df.empty:
            print("无候选。")
        else:
            print(df[["vt_symbol", "latest_date", "close", "score", "label", "strategy_tags"]].to_string(index=False))
        return 0

    if args.command == "radar-config":
        path = save_config(
            small_account_config(account_equity=args.account_equity),
            args.out or "committee_config/radar_small_account.json",
        )
        print(f"已生成小资金雷达配置: {path}")
        if args.print_radar_config:
            print(Path(path).read_text(encoding="utf-8"))
        return 0

    if args.command == "market-trend":
        targets = select_targets(args.targets)
        start_date = args.start or default_history_start(args.years)
        results = evaluate_targets(
            targets=targets,
            start_date=start_date,
            end_date=args.end or None,
            evaluate_date=args.evaluate_date or None,
            lookback=args.lookback,
            event=args.event or None,
            use_cache=not args.no_cache,
            force=args.force_history,
            use_param_cache=not args.no_param_cache,
            force_params=args.force_params,
        )
        json_path, md_path = save_market_trend_review(results, output_dir=args.output_dir)
        total = len([result for result in results if result.correct is not None])
        hits = len([result for result in results if result.correct])
        print(f"已生成A股趋势核验报告: {md_path}")
        print(f"JSON: {json_path}")
        if total:
            print(f"可核验 {total} 个标的，命中 {hits} 个，命中率 {hits / total:.1%}")
        else:
            print("已生成下一交易日趋势信号，暂无实际涨跌可核验。")
        if args.print_market_trend:
            print()
            print(market_trend_markdown(results))
        return 0

    if args.command == "market-regime":
        start_date = args.start or default_history_start(args.years)
        results = evaluate_targets(
            targets=DEFAULT_TARGETS,
            start_date=start_date,
            end_date=args.end or None,
            lookback=args.lookback,
            event=args.event or None,
            use_cache=not args.no_cache,
            force=args.force_history,
            use_param_cache=not args.no_param_cache,
            force_params=args.force_params,
        )
        regime = classify_market_regime(results)
        json_path, md_path = save_market_regime_report(regime, results, output_dir=args.output_dir)
        print(f"已生成A股市场环境报告: {md_path}")
        print(f"JSON: {json_path}")
        print(f"环境: {regime.label} | 风险闸门: {regime.risk_gate}")
        if args.print_market_regime:
            print()
            print(market_regime_markdown(regime, results))
        return 0

    if args.command == "trend-history-cache":
        targets = select_targets(args.targets)
        start_date = args.start or default_history_start(args.years)
        end_date = args.end or None
        if args.refresh or args.force:
            entries = backfill_trend_history(
                targets=targets,
                start_date=start_date,
                end_date=end_date,
                years=args.years,
                force=args.force,
            )
        else:
            entries = trend_history_cache_summary()
        errors = len([entry for entry in entries if entry.error])
        ok = len(entries) - errors
        print(f"趋势历史缓存: {ok} 个可用，{errors} 个异常")
        if args.print_history_cache:
            print()
            print(trend_history_cache_markdown(entries))
        elif entries:
            rows = [
                {
                    "name": entry.name,
                    "symbol": entry.symbol,
                    "rows": entry.rows,
                    "start": entry.start,
                    "end": entry.end,
                    "error": entry.error,
                }
                for entry in entries
            ]
            import pandas as pd

            print(pd.DataFrame(rows).to_string(index=False))
        return 0

    if args.command == "catalyst-scan":
        items = _load_news_items(args.text, args.file, args.inbox_dir)
        if not items:
            items = fetch_realtime_news(limit=args.limit)
        hits = scan_catalysts(items, threshold=args.threshold)
        csv_path, md_path = save_catalyst_report(hits, output_dir=args.output_dir)
        print(f"已生成催化剂扫描报告: {md_path}")
        print(f"CSV: {csv_path}")
        print(f"高分催化剂: {len(hits)} 条")
        if args.print_catalysts:
            print()
            print(catalyst_markdown(hits))
        return 0

    if args.command == "risk-radar":
        items = _load_news_items(args.text, args.file, args.inbox_dir)
        symbols = _normalize_symbol_codes(_load_symbols(args.symbols, args.symbol_file))
        if symbols:
            items.extend(fetch_symbol_notice_items(symbols, days=args.notice_days))
            items.extend(fetch_symbol_risk_items(symbols))
        if not items:
            items = fetch_realtime_news(limit=args.limit)
        signals = scan_risk_items(items, threshold=args.threshold)
        if args.red_only:
            signals = [signal for signal in signals if signal.severity.startswith("红灯")]
        if args.new_event_only:
            signals = [signal for signal in signals if signal.risk_type != "存量风险状态"]
        csv_path, md_path = save_risk_report(signals, output_dir=args.output_dir)
        print(f"已生成暴雷风险报告: {md_path}")
        print(f"CSV: {csv_path}")
        print(f"风险信号: {len(signals)} 条")
        if args.print_risk:
            print()
            print(risk_markdown(signals))
        return 0

    if args.command == "event-radar":
        items = _load_news_items(args.text, args.file, args.inbox_dir)
        if not items:
            items = fetch_realtime_news(limit=args.limit)
        hits = scan_catalysts(items, threshold=args.threshold)
        snapshots = fetch_etf_snapshots(
            KNOWN_TARGETS,
            cache_seconds=args.cache_seconds,
            force=args.force_spot,
        )
        breadth = classify_intraday_breadth(snapshots)
        selected_hits, scores = build_event_radar(hits, snapshots, top_events=args.top_events)
        regime = None if args.ignore_regime else load_latest_market_regime(args.output_dir)
        hits_csv, scores_csv, md_path = save_event_radar_report(
            selected_hits,
            scores,
            regime=regime,
            breadth=breadth,
            output_dir=args.output_dir,
        )
        print(f"已生成事件雷达报告: {md_path}")
        print(f"催化剂CSV: {hits_csv}")
        print(f"Alpha CSV: {scores_csv}")
        print(f"高分催化剂: {len(selected_hits)} 条")
        if regime is not None:
            print(f"环境闸门: {regime.label} | {regime.risk_gate}")
        print(f"盘中强度: {breadth.label} | {breadth.risk_gate}")
        if selected_hits and not args.no_log:
            event_text = "\n".join(hit.title for hit in selected_hits)
            log_path = append_alpha_log(scores, selected_hits, event_text=event_text)
            print(f"信号日志: {log_path}")
        if args.print_event_radar:
            print()
            print(event_radar_markdown(selected_hits, scores, regime=regime, breadth=breadth))
        return 0

    if args.command == "event-alpha":
        hits = score_manual_event(args.text, threshold=args.threshold)
        snapshots = fetch_etf_snapshots(
            KNOWN_TARGETS,
            cache_seconds=args.cache_seconds,
            force=args.force_spot,
        )
        scores = rank_event_reactions(hits, snapshots, targets=KNOWN_TARGETS)
        csv_path, md_path = save_alpha_report(scores, output_dir=args.output_dir)
        print(f"已生成事件信号栈报告: {md_path}")
        print(f"CSV: {csv_path}")
        if not args.no_log:
            log_path = append_alpha_log(scores, hits, event_text=args.text)
            print(f"信号日志: {log_path}")
        if args.print_alpha:
            print()
            print(alpha_markdown(scores))
        return 0

    if args.command == "event-alpha-evaluate":
        snapshots = fetch_etf_snapshots(
            KNOWN_TARGETS,
            cache_seconds=args.cache_seconds,
            force=args.force_spot,
        )
        outcomes = evaluate_alpha_log(snapshots, log_path=args.log, limit=args.limit)
        csv_path, md_path = save_alpha_outcome_report(outcomes, output_dir=args.output_dir)
        print(f"已生成事件信号结果评估: {md_path}")
        print(f"CSV: {csv_path}")
        if args.print_alpha_eval:
            print()
            print(alpha_outcome_markdown(outcomes))
        return 0

    if args.command == "event-alpha-feedback":
        snapshots = fetch_etf_snapshots(
            KNOWN_TARGETS,
            cache_seconds=args.cache_seconds,
            force=args.force_spot,
        )
        outcomes = evaluate_alpha_log(snapshots, log_path=args.log, limit=args.limit)
        feedback = summarize_alpha_feedback(outcomes, min_samples=args.min_samples)
        csv_path, md_path = save_alpha_feedback_report(feedback, output_dir=args.output_dir)
        print(f"已生成事件Alpha参数反馈: {md_path}")
        print(f"CSV: {csv_path}")
        if args.print_alpha_feedback:
            print()
            print(alpha_feedback_markdown(feedback))
        return 0

    parser.error("未知命令")
    return 2


def _load_symbols(symbols_text: str, file_path: str) -> list[str]:
    symbols = []
    if symbols_text:
        symbols.extend(_split_csv(symbols_text))
    if file_path:
        path = Path(file_path)
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if item and not item.startswith("#"):
                symbols.append(item)
    return symbols


def _split_csv(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _build_external_risk_flags(symbols: list[str], days: int = 120) -> dict[str, list[str]]:
    codes = _normalize_symbol_codes(symbols)
    if not codes:
        return {}
    items = fetch_symbol_notice_items(codes, days=days)
    items.extend(fetch_symbol_risk_items(codes))
    signals = scan_risk_items(items, threshold=5.0)
    return symbol_risk_flags(signals)


def _normalize_symbol_codes(symbols: list[str]) -> list[str]:
    codes = []
    for symbol in symbols:
        try:
            code = normalize_symbol(symbol).code
        except ValueError:
            code = str(symbol).strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _load_news_items(text: str, file_path: str, inbox_dir: str = "") -> list[NewsItem]:
    items: list[NewsItem] = []
    if text:
        items.append(NewsItem(title=text, source="manual"))
    if file_path:
        path = Path(file_path)
        file_text = path.read_text(encoding="utf-8")
        items.append(NewsItem(title=path.name, summary=file_text, source="file"))
    if inbox_dir:
        inbox = Path(inbox_dir)
        paths = [
            path
            for path in inbox.glob("*")
            if path.is_file() and path.suffix.lower() in {".txt", ".md"}
        ]
        for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
            stat = path.stat()
            published = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            items.append(
                NewsItem(
                    title=path.name,
                    summary=path.read_text(encoding="utf-8"),
                    published_at=published,
                    source="inbox",
                    url=str(path),
                )
            )
    return items


if __name__ == "__main__":
    raise SystemExit(main())
