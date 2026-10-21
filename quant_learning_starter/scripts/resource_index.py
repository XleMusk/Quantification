"""Render the supplied resource catalog without changing its original URLs."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
catalog = json.loads((ROOT / "resources.json").read_text(encoding="utf-8"))
checks = json.loads((ROOT / "outputs" / "source_checks.json").read_text(encoding="utf-8"))
resources = catalog["resources"]
if len({r["id"] for r in resources}) != len(resources):
    raise ValueError("Duplicate resource IDs")
rows = ["# 学习资料索引", "", "原包 23 项资源全部保留。下面状态只代表本轮查阅范围，不代表已读完或已学完。",
        "", "本轮访问 8 个重点入口：7 个可读取，1 个失败；其余 15 项仅保留原包记录，未重新核验。",
        "", "| ID | 资料 | 优先级 | 本轮状态 |", "|---|---|---|---|"]
for resource in resources:
    state = checks["checks"].get(resource["id"], {"status": "supplied_not_rechecked"})
    rows.append(f"| {resource['id']} | [{resource['title']}]({resource['official_url']}) | {resource['priority']} | {state['status']} |")
rows += ["", "## 先用这几项", "", "今天的阅读与计算对应关系见 [START_TODAY](../START_TODAY.md)。", ""]
for key, check in checks["checks"].items():
    rows.append(f"- {key}: [{check['status']}]({check['url']})。{check['note']}")
rows += ["", "当前练习使用原创合成数据，无需下载外部书籍、视频或行情。没有批量获取课程、付费正文或上游仓库。", ""]
(ROOT / "outputs" / "resource_index.md").write_text("\n".join(rows), encoding="utf-8")
print(f"Indexed {len(resources)} resources; recorded {len(checks['checks'])} live entry checks")
