"""Generate the local resource index from the supplied list and observed receipts."""

import html
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def link(label, url):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def main():
    manifest = json.loads((ROOT / "source_bundle" / "official_resources.json").read_text(encoding="utf-8"))
    downloaded = json.loads((ROOT / "starter_download_status.json").read_text(encoding="utf-8"))["results"]
    reviews = json.loads((ROOT / "book_access_review.json").read_text(encoding="utf-8"))
    practice = json.loads((ROOT / "practice" / "output" / "result.json").read_text(encoding="utf-8"))
    assets = {a["path"]: a for a in downloaded if a["status"] in ("downloaded", "existing_validated")}
    rows = []
    for section in ("books", "courses", "projects"):
        for item in manifest[section]:
            row = {"category": section, "id": item["id"], "order": item["order"],
                   "title": item.get("title", item.get("name")), "url": item["url"],
                   "focus": item.get("focus", item.get("task")), "local_files": [],
                   "status": "link_saved", "note": "入口已保留；未下载或安装，未逐项重新核验。"}
            if section == "books":
                row["note"] = item["access"]
                if item["id"] in reviews:
                    row.update(status=reviews[item["id"]]["status"], note=reviews[item["id"]]["note"])
                elif item["id"] == "pydata":
                    row.update(status="online_html", note="作者 HTML 全文入口已保留，未批量镜像或转成 PDF。")
                else:
                    row.update(status="purchase_or_library", note="商业书正版入口已保留，需已有正版或图书馆授权；未购买或获取正文。")
                if item.get("reader_url"):
                    row["reader_url"] = item["reader_url"]
            elif section == "courses":
                selected = {"mit15401": ["courses/MIT15401_Equities.pdf", "courses/MIT15401_Risk_and_Return.pdf"],
                            "mit1805": ["courses/MIT1805_Covariance_and_Correlation.pdf"]}.get(item["id"], [])
                row["local_files"] = [p for p in selected if p in assets]
                if row["local_files"]:
                    row.update(status="selected_notes_downloaded", note="指定讲义已下载并解析；视频只保存官方链接。")
                else:
                    row["note"] = "课程入口已保存，未下载课程视频。" + item["notes"]
                row["other_links"] = {k: item[k] for k in ("video_url", "materials_url", "python_labs_video_url") if item.get(k)}
            elif item["id"] == "pydata":
                row["local_files"] = [p for p in assets if p.startswith("code/pydata-book/")]
                row.update(status="selected_notebooks_downloaded", note="第 5/6/7/8/10/11 章已缓存，含许可证；只校验格式，未执行上游 Notebook。部分示例需额外数据或依赖。")
            elif item["id"] == "ml4t":
                row["note"] = "第二版入口已固定为 second-edition；未克隆或安装。"
            rows.append(row)

    timestamp = datetime.now().astimezone().isoformat()
    record = {"prepared_at": timestamp, "execution_location": "local", "remote_workspace": "not confirmed; no cloud actions",
              "counts": {"books": 8, "courses": 8, "projects": 9,
                         "book_pdfs_downloaded": 0, "lecture_pdfs_downloaded": sum(a["kind"] == "pdf" for a in assets.values()),
                         "notebooks_downloaded": sum(a["kind"] == "notebook" for a in assets.values()),
                         "downloaded_bytes": sum(a["bytes"] for a in assets.values())},
              "practice": practice, "resources": rows, "artifacts": list(assets.values())}
    for path in assets:
        if not (ROOT / path).is_file():
            raise FileNotFoundError(path)
    (ROOT / "preparation_status.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    labels = {"link_saved": "入口已备", "online_html": "在线 HTML", "purchase_or_library": "需正版 / 图书馆",
              "online_readable_download_unavailable": "在线可读，未下载", "download_failed": "下载未成功",
              "selected_notes_downloaded": "讲义已备", "selected_notebooks_downloaded": "Notebook 已备"}
    counts = record["counts"]
    status_text = f'25 项入口已整理；{counts["notebooks_downloaded"]} 份 Notebook、{counts["lecture_pdfs_downloaded"]} 份讲义已保存。三本书籍 PDF 尚未取得，四本商业书保留正版入口。'
    sections = []
    for category, heading in (("books", "书籍"), ("courses", "课程"), ("projects", "项目")):
        body = []
        for row in [r for r in rows if r["category"] == category]:
            links = [link("官方入口", row["url"])]
            if row.get("reader_url"):
                links.append(link("在线阅读", row["reader_url"]))
            for name, url in row.get("other_links", {}).items():
                links.append(link({"video_url": "视频目录", "materials_url": "讲义目录", "python_labs_video_url": "Python 实验视频"}[name], url))
            links.extend(link(Path(p).name, p) for p in row["local_files"])
            body.append(f'<tr><td>{html.escape(str(row["order"]))}</td><td><strong>{html.escape(row["title"])}</strong><p>{html.escape(row["focus"])}</p></td>'
                        f'<td><span class="status">{html.escape(labels[row["status"]])}</span><p>{html.escape(row["note"])}</p></td><td class="links">' + "<br>".join(links) + '</td></tr>')
        sections.append(f'<section><h2>{heading}</h2><div class="table-wrap"><table><thead><tr><th>顺序</th><th>资料与选读范围</th><th>准备状态</th><th>打开</th></tr></thead><tbody>{"".join(body)}</tbody></table></div></section>')
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化学习资料库</title><style>
*{box-sizing:border-box}body{margin:0;color:#23252a;background:#fafafa;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;letter-spacing:0}
main{max-width:1220px;margin:auto;padding:28px 22px}h1{font-size:28px;margin:0 0 12px}h2{font-size:21px;margin:30px 0 10px}
a{color:#165c9c;text-underline-offset:3px;overflow-wrap:anywhere}p{margin:6px 0;font-weight:400}header{padding-bottom:20px;border-bottom:2px solid #258077}
.summary{background:#eef5f2;border-left:4px solid #258077;padding:12px 16px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;background:white}
th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #dedede}th{background:#eee;font-size:13px}td:first-child{width:58px}td:nth-child(2){width:34%}td:nth-child(3){width:36%}
.status{font-weight:700;color:#743f22}.links{min-width:180px;font-size:13px}footer{margin-top:28px;font-size:13px;color:#555}
@media(max-width:620px){main{padding:18px 12px}h1{font-size:24px}table{min-width:740px}}
</style></head><body><main><header><h1>量化学习资料库</h1>'''
    page += f'<p class="summary">{html.escape(status_text)}</p><p>基础练习：{link("CSV → 合并 → 分组 → 滚动 → Parquet", "practice/README.md")} · {link("准备记录", "PREPARATION_REPORT.md")} · {link("原始详细清单", "source_bundle/READING_ORDER.md")}</p></header>'
    page += "".join(sections)
    page += f'<footer>准备时间：{html.escape(timestamp)}。本地只保存资料与教学结果；源 Notebook 的既有输出不是本次运行证据。未新建云资源或上传文件，未安装清单中的其他框架。</footer></main></body></html>'
    (ROOT / "RESOURCE_NAVIGATION.html").write_text(page, encoding="utf-8")
    report = f'''# 量化资料准备记录

{status_text}

## 实际成功
- 下载 pandas 第 5/6/7/8/10/11 章共六份 Notebook，并保留 COPYING 和 README。
- 上游代码固定在 3rd-edition 分支提交 a892791ce5a3831134cfa2989033ecbb9289f1ea。
- MIT 15.401 Equities（24 页）、Risk and Return（36 页）、MIT 18.05 Covariance and Correlation（10 页）已下载并通过 PDF 解析。
- 下载文件合计 {counts["downloaded_bytes"]:,} 字节；无整套视频、全量仓库或行情数据。
- 原创基础练习已运行：两只虚构股票、八个模拟交易日、16 条记录，五项核对通过。

## 实际失败或只能在线使用
'''
    for key in ("probability", "islp", "convex"):
        report += f'- {key}: {reviews[key]["note"]}\n'
    report += '''
## 尚未执行
- 四本商业书仅保留出版社/作者入口，未购买或获取正文。
- Python for Data Analysis 只保留作者 HTML 链接，未整站抓取。
- 课程视频只保留入口；其他项目只保存顺序和入口，未安装、未运行。
- 六份上游 Notebook 只校验文件结构，未逐单元执行，未取其全部示例数据与可选依赖。
- 本项目已有合成因子 Notebook 仍在原位置，未改写原 16 周计划。

## 使用
打开 RESOURCE_NAVIGATION.html，可直接进入本地讲义、Notebook 和在线资源。
基础练习命令：

```powershell
D:\\Quantification\\quant_learning_starter\\.venv\\Scripts\\python.exe D:\\Quantification\\quant_learning_starter\\learning_resources\\practice\\data_handling.py
```

练习对应 pandas 第 6/8/10/11 章，结果在 practice/output/，明确标记 synthetic。
原包中的历史下载失败声明保存在 source_bundle；此次真实结果见 books/download_status.json、starter_download_status.json、book_access_review.json 和 preparation_status.json。
教材和讲义按原作者/机构的使用条款仅供个人学习备查，不随项目代码提交；许可证说明见 LICENSE_NOTES.md。
'''
    (ROOT / "PREPARATION_REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps(record["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
