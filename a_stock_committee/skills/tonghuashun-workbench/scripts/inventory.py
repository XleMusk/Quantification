"""Index allowlisted public THS help/configuration; never connect to its UI."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Unsupported encoding: {path}")


def xml_root(path: Path) -> ET.Element:
    return ET.fromstring(read_text(path))


class ShortcutTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cells: list[str] = []
        self.parts: list[str] = []
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "tr":
            self.cells = []
        elif tag == "td":
            self.in_cell = True
            self.parts = []
        elif tag == "br" and self.in_cell:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self.in_cell = False
            self.cells.append(" ".join("".join(self.parts).split()))
        elif tag == "tr" and len(self.cells) >= 2:
            self.rows.append(self.cells[:])


def inventory(root: Path) -> dict:
    base = root / "system" / "同花顺方案"
    toolbar: dict[tuple, dict] = {}
    for path in sorted((base / "ToolBar").glob("*.xml")):
        for group in xml_root(path).findall("group"):
            for button in group.findall("button"):
                name = (button.findtext("name") or "").strip()
                tip = (button.findtext("tip") or "").strip()
                key = (group.get("name", ""), name, tip)
                record = toolbar.setdefault(key, {
                    "group": key[0], "name": name, "tip": tip, "sources": [],
                })
                record["sources"].append(str(path.relative_to(root)))

    shortcuts_path = root / "html" / "shortKey" / "index.html"
    parser = ShortcutTable()
    parser.feed(read_text(shortcuts_path))
    shortcuts = []
    seen = set()
    for cells in parser.rows:
        for offset in range(0, len(cells) - 1, 2):
            key = (cells[offset], cells[offset + 1])
            if key not in seen and all(key):
                seen.add(key)
                shortcuts.append({"key": key[0], "action": key[1]})

    categories = []
    for category in xml_root(base / "hotkey.xml").findall("category"):
        for entry in category.findall("hotkeyitem"):
            categories.append({"category": category.get("name"), "name": entry.get("name")})

    drawing: dict[str, dict] = {}
    for filename in ("DrawLineBar.xml", "DrawLineBarBase.xml"):
        for item in xml_root(base / filename).findall("item"):
            # Menu is a documented local tuple list, not executable commands.
            for definition in (item.findtext("Menu") or "").split(";"):
                name = definition.split(",", 1)[0].strip()
                if name:
                    record = drawing.setdefault(name, {"name": name, "groups": []})
                    group = item.get("name", "")
                    if group not in record["groups"]:
                        record["groups"].append(group)

    functions = []
    for path in sorted((base / "func_help").glob("*.xml")):
        func = xml_root(path)
        functions.append({
            "symbol": func.findtext("en_name") or path.stem,
            "name": func.findtext("cn_name") or "",
            "scope": func.findtext("use_scope") or "",
            "source": str(path.relative_to(root)),
        })

    fields = set()
    def collect(value: object) -> None:
        if isinstance(value, dict):
            name = value.get("Name")
            if isinstance(name, str):
                fields.add(name)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
    collect(json.loads(read_text(base / "Entrancetoolbar.json")))

    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "installation": str(root.resolve()),
        "verification": "Public configuration/help only; UI actions and entitlements untested.",
        "toolbar": list(toolbar.values()), "shortcuts": shortcuts,
        "function_entries": categories, "drawing": list(drawing.values()),
        "formula_functions": functions, "quote_fields": sorted(fields),
    }
    result["counts"] = {key: len(result[key]) for key in (
        "toolbar", "shortcuts", "function_entries", "drawing", "formula_functions", "quote_fields",
    )}
    if any(value == 0 for value in result["counts"].values()):
        raise ValueError(f"Incomplete public catalog: {result['counts']}")
    return result


def markdown(data: dict) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["# 同花顺本机功能索引", "", f"生成时间 (UTC): {data['created_at']}",
             "", "仅证明公共配置或帮助文件包含该条目，不证明当前界面可用、已订阅或已实机测试。",
             "快捷键仅供查找，必须先核对窗口焦点；交易相关快捷键不得自动试按。", ""]
    def table(title: str, headers: list[str], rows: list[list]) -> None:
        lines.extend([f"## {title}", "", "| " + " | ".join(headers) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"])
        lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
        lines.append("")
    table("工具栏", ["分组", "按钮", "本机提示"],
          [[x["group"], x["name"], x["tip"]] for x in data["toolbar"]])
    table("快捷键", ["键", "本机帮助的功能"],
          [[x["key"], x["action"]] for x in data["shortcuts"]])
    table("功能入口", ["分类", "名称"],
          [[x["category"], x["name"]] for x in data["function_entries"]])
    table("画线与测量工具", ["名称", "所属分组"],
          [[x["name"], ", ".join(x["groups"])] for x in data["drawing"]])
    table("报价字段", ["字段名"], [[x] for x in data["quote_fields"]])
    table("公式函数目录", ["函数", "名称", "适用周期", "本机帮助路径"],
          [[x["symbol"], x["name"], x["scope"], x["source"]] for x in data["formula_functions"]])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output == root or root in output.parents:
        parser.error("Output must be outside the THS installation.")
    data = inventory(root)
    output.mkdir(parents=True, exist_ok=True)
    (output / "catalog.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "catalog.md").write_text(markdown(data), encoding="utf-8")
    print(json.dumps({"output": str(output), "counts": data["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
