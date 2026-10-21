"""Publish a coverage-first local report for the public disclosure dataset."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pandas as pd

from ..settings import ROOT
from .seat_dataset import DEFAULT_DATASET, export_catalogs, inspect_dataset
from .seats import DEFAULT_HOME, save_json


def publish(root=DEFAULT_DATASET, out=None):
    root = Path(root)
    out = Path(out) if out else ROOT / "outputs" / "seat_dataset" / datetime.now().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    status = inspect_dataset(root)
    with closing(sqlite3.connect(root / "disclosures.sqlite3")) as db:
        export_catalogs(db, root)
        status["a_share_identifiable_seat_pairs"] = db.execute("SELECT COUNT(*) FROM a_share_seat_registry WHERE identity_usable=1").fetchone()[0]
        status["stock_summary_date_range"] = db.execute("SELECT MIN(trade_date),MAX(trade_date) FROM stock_disclosures").fetchone()
        status["known_one_side_only_rows"] = db.execute("SELECT COUNT(*) FROM seat_observations WHERE (buy_cny IS NULL AND sell_cny IS NOT NULL) OR (sell_cny IS NULL AND buy_cny IS NOT NULL)").fetchone()[0]
        status["generic_identity_rows"] = db.execute("SELECT COUNT(*) FROM seat_observations WHERE identity_usable=0").fetchone()[0]
        status["conflicting_complete_amount_groups"] = db.execute("SELECT COUNT(*) FROM event_amount_conflicts").fetchone()[0]
        status["securities_by_kind"] = dict(db.execute("SELECT security_kind,COUNT(*) FROM security_registry GROUP BY security_kind").fetchall())
        status["media_person_labels"] = db.execute("SELECT COUNT(DISTINCT person_label) FROM person_seat_links").fetchone()[0]
    inventory = pd.read_csv(root / "catalogs/stock_inventory.csv")
    status["a_share_stocks_without_seat_details"] = int(inventory.downloaded_seat_rows.eq(0).sum())
    status["a_share_stock_days_without_details"] = len(pd.read_csv(root / "catalogs/a_share_stock_days_without_details.csv"))
    status["report_created_at"] = datetime.now().astimezone().isoformat()
    integrity = root / "summary_integrity.json"
    status["summary_integrity"] = json.loads(integrity.read_text(encoding="utf-8")) if integrity.exists() else None
    seat_integrity = root / "seat_integrity.json"
    status["seat_integrity"] = json.loads(seat_integrity.read_text(encoding="utf-8")) if seat_integrity.exists() else None
    save_json(out / "coverage_snapshot.json", status)
    summary = status["coverage"].get("summary", {})
    summary_complete = (summary.get("downloaded_rows") == summary.get("expected_rows")
                        and status["summary_integrity"] and status["summary_integrity"]["status"] == "source_pagination_verified")
    remaining = status["raw_rows_expected"] - status["raw_rows_downloaded"]
    date_range = status["downloaded_seat_date_range"]
    lines = ["# A股公开席位研究数据集", "",
        f"报告时间：{status['report_created_at']}。状态：`{status['status']}`。", "",
        "**这是公开龙虎榜研究库，不是任何游资的私人完整交割单。没有上榜的交易、账户关联、真实持仓和收益无法从本库还原。**", "",
        "## 已取得的数据", "",
        "| 层级 | 已入库 | 覆盖/状态 |", "|---|---:|---|",
        f"| 源端全部证券榜单汇总 | {status['stock_disclosures']:,} 条 | {'全部源端分页及唯一标识核对通过' if summary_complete else '尚未完成或需核对'} |",
        f"| 其中普通A股榜单 | {status['a_share_summary_rows']:,} 条 | {status['a_share_summary_stocks']:,} 个历史市场代码 |",
        f"| 源端买卖方席位列示 | {status['seat_observations']:,} 条 | 原始列示，不等于交易笔数 |",
        f"| 其中普通A股席位列示 | {status['a_share_seat_observations']:,} 条 | {status['observed_a_share_stock_count']:,} 只股票 |",
        f"| 可识别A股席位原名/代码组合 | {status['a_share_identifiable_seat_pairs']:,} 组 | 不等于人数或账户数 |",
        f"| 人物关联种子 | {status['person_seat_links']} 条 | {status['media_person_labels']} 个媒体称呼，均未确认账户身份 |", "",
        f"股票级汇总观测范围：{status['stock_summary_date_range'][0]} 至 {status['stock_summary_date_range'][1]}。",
        f"当前席位明细观测范围：{date_range[0]} 至 {date_range[1]}；具体股票和日期的缺口见覆盖清单。",
        f"源端计划原始总量：{status['raw_rows_expected']:,}；已入库：{status['raw_rows_downloaded']:,}；尚未回补：{remaining:,}。此统计含B股、债券和CDR，A股视图已分别过滤。", "",
        "**即使全部源端分页到齐，也只代表这个供应商快照到齐，不代表交易所历史披露经独立穷尽核对，更不代表所有游资全部交易可见。**", "",
        f"席位分页核对：`{status['seat_integrity']['status'] if status['seat_integrity'] else 'not_checked'}`。跨页重复仅在单独查询整个榜单、逐项及重复次数完全一致后，才可认定为源端匿名机构的重复列示；证据保存在seat_integrity.json及overlap_reviews。所有原始行保留，不据此推断个人交易。", "",
        "## 本地入口", "",
        f"- 数据库：[{root.name}/disclosures.sqlite3]({(root / 'disclosures.sqlite3').as_posix()})",
        f"- [股票全目录]({(root / 'catalogs/stock_inventory.csv').as_posix()})：列出汇总范围、当前已有席位行数。",
        f"- [A股席位目录]({(root / 'catalogs/a_share_seat_registry.csv').as_posix()})。",
        f"- [逐席位披露概况]({(root / 'catalogs/seat_disclosure_profiles.csv').as_posix()})：出现日、股票数、已知买卖方向，不计算个人胜率。",
        f"- [逐日覆盖表]({(root / 'catalogs/date_coverage.csv').as_posix()})。",
        f"- [有榜单但本库无明细的股票日]({(root / 'catalogs/a_share_stock_days_without_details.csv').as_posix()})：{status['a_share_stock_days_without_details']:,} 个；完全没有席位明细的股票 {status['a_share_stocks_without_seat_details']:,} 只。未回补或源端缺失不等于没有交易。",
        f"- [人物关联及出处]({(root / 'catalogs/person_seat_links.csv').as_posix()})。",
        f"- [数据字典与命令]({(ROOT / 'trading/research/SEAT_DATASET.md').as_posix()})。", "",
        f"- [逐年Parquet清单]({(root / 'parquet/manifest.json').as_posix()})：普通A股事实表，保留原始列示和缺失值。" if (root / 'parquet/manifest.json').exists() else "Parquet尚未发布；需先完成源端分页和明细完整性检查。", "",
        "## 数据约束", "",
        f"有 {status['known_one_side_only_rows']:,} 条原始列示仅披露一侧金额：已知金额保留，另一侧和净额为空，不补零。",
        f"有 {status['generic_identity_rows']:,} 条泛称或缺少可识别代码的列示，不合并成同一个投资者；完整金额冲突组 {status['conflicting_complete_amount_groups']:,} 组。",
        "单日榜和累计榜分别保存；不能确认累计起点时留空。同一已知席位相同金额的重复列示不重复加总。",
        "营业部变更候选、媒体标签、证券简称均保留出处；供应商回填的名称不冒充已核实的历史时点名称。",
        f"北交所开市前的 {status['pre_bse_summary_rows']} 条榜单单列保存，不计入普通A股视图。证券代码也可能被回填为新代码；股票数量以来源市场代码计，未完成跨市场转板与历史代码的统一公司身份核对。",
        "历史发布时间缺失时保持空值，采集时间不冒充披露时间。原始响应中的事后涨幅和席位胜率字段不进入历史信号特征。", "",
        "v2使用稳定排序，股票汇总复用已核对原件。旧版席位分页发现跨页重复，保留在原目录并标记SUPERSEDED，不混入修正版。", "",
        "## 还缺什么", "",
        f"- 剩余 {remaining:,} 条源端列示的回补及明细层覆盖检查。" if remaining else "- 源端分页已到齐；仍需交易所/另一来源的完整性抽查与历史时点核对。",
        "- 人物与全部席位关系不能穷尽确认，当前媒体种子远非完整名录。",
        "- 全市场配套日线、历史证券状态、行业和公司行为尚未补齐；当前统一账本验证过的日线仍为10股试点。",
        "- 私人交割单只有在持有人合法提供并明确研究权限后才能另行导入；目前没有此类材料。", "",
        "可授权补充：Tushare [hm_list](https://tushare.pro/document/2?doc_id=311) 名录需5000积分，[hm_detail](https://tushare.pro/document/2?doc_id=312) 逐日明细需10000积分且历史从2022年8月起。已核对文档，未接入或购买；它们仍是供应商分类，不是私人完整账户记录。", "",
        "## 本轮其他交付", "",
        "资料包已保留在外部数据目录，8条报道样例逐条对照原始列示。5条满足预设金额精度；永辉超市差65.40元，另两条银河大连黄河路约数与原始买/卖额差约111.41万元、86.46万元，保留口径待核对，未强行改成一致。",
        f"[样例核对表]({(DEFAULT_HOME / 'example_verification/verified_public_examples.csv').as_posix()})。",
        "10股Donchian对照已实际回放：纯趋势净收益约-22.17%，目标席位买入过滤无交易；这不是席位策略有效的证据。旧负结果保留，未发布策略或进行实盘操作。", "",
        "## 续传与查询", "", "```powershell",
        ".\\env\\python.exe -X utf8 run_committee.py seat-dataset --status",
        ".\\env\\python.exe -X utf8 run_committee.py seat-dataset --kinds buy sell --max-requests 6000 --workers 6 --execute",
        ".\\env\\python.exe -X utf8 -m trading.research.seat_dataset_audit",
        ".\\env\\python.exe -X utf8 run_committee.py seat-dataset --export-stock 600127.SSE --out D:\\QuantificationData\\trading\\research\\seat_trend\\exports\\600127_disclosures.csv",
        "```", "",
        "原始来源：[东方财富龙虎榜](https://data.eastmoney.com/stock/lhb.html)；接口参考：[AKShare](https://akshare.akfamily.xyz/data/stock/stock.html)、[Tushare字段说明](https://tushare.pro/document/2?doc_id=107)。媒体种子来源：[财联社2025-05-19报道](https://www.cls.cn/detail/2035101)。数据保持本地研究用途，不假定有公开再分发许可。", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(out / "REPORT.md")
    return out


if __name__ == "__main__":
    publish()
