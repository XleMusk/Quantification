"""518880's closed issuer distribution table, archived as full web-tool text."""
import argparse
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

from .etf_reconciliation import archived
from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from ..settings import DATA_ROOT


CODE = "518880"
PAGE = "https://www.huaan.com.cn/funds/518880/index.shtml"
PCF = "https://www.huaan.com.cn/etf/518880/sgshqd.jsp"
KIND = "huaan_public_distribution_table_v1"
NAME = "\u534e\u5b89\u6613\u5bcc\u9ec4\u91d1\u4ea4\u6613\u578b\u5f00\u653e\u5f0f\u8bc1\u5238\u6295\u8d44\u57fa\u91d1"
HEADERS = ["\u5e74\u4efd", "\u5206\u914d\u65b9\u6848", "\u767b\u8bb0\u65e5", "\u9664\u606f\u65e5", "\u5206\u914d\u65b9\u5f0f",
           "\u7ea2\u5229\u8f6c\u518d\u6295\u8d44\u65e5", "\u518d\u6295\u8d44\u8d4e\u56de\u8d77\u59cb\u65e5"]
SPLIT = ["2013", "1\u62980.37761358", "2013-07-24", "2013-07-24", "\u62c6\u5206", "2013-07-24", "2013-07-24"]


def full_lines(raw, url):
    text = raw.decode("utf-8")
    header = re.search(r"Source: open\((\{[^\n]*\})\); Total lines: (\d+)\n", text)
    if not header or json.loads(header[1]) != {"ref_id": url, "lineno": None}:
        raise ValueError("Expected the full named primary page extraction, not a search snippet or HTML claim")
    matches = list(re.finditer(r"\bL(\d+):", text[header.end():]))
    body = text[header.end():]
    if [int(match[1]) for match in matches] != list(range(int(header[2]))):
        raise ValueError("Primary page extraction is truncated, repeated or out of order")
    return [re.sub(r"\ue200cite\ue202[^\ue201]*\ue201", "", body[match.end():
             matches[i + 1].start() if i + 1 < len(matches) else len(body)]).strip()
            for i, match in enumerate(matches)]


def parse(page, pcf):
    lines = full_lines(page, PAGE)
    identity = [line for line in lines if "\u6cd5\u5b9a\u540d\u79f0" in line and "\u57fa\u91d1\u4ee3\u7801" in line]
    if len(identity) != 1 or [cell.strip() for cell in identity[0].split("|")] != ["\u6cd5\u5b9a\u540d\u79f0", NAME, "\u57fa\u91d1\u4ee3\u7801", CODE]:
        raise ValueError("Huaan primary page identifies a different share class/fund")
    dates = [i for i, line in enumerate(lines) if line == "* \u65e5\u671f"]
    if len(dates) != 1:
        raise ValueError("Missing independent full-year fund NAV date")
    fields = [line for line in lines[dates[0] + 1:] if line][:4]
    if len(fields) != 4 or fields[1] != "* \u5355\u4f4d\u51c0\u503c" or fields[3] != "* \u7d2f\u8ba1\u51c0\u503c":
        raise ValueError("Changed issuer NAV fields")
    as_of, nav = date.fromisoformat(fields[0]).isoformat(), Decimal(fields[2])
    pcf_lines = full_lines(pcf, PCF)
    pcf_text = " ".join(pcf_lines)
    prior = re.search(r"\u57fa\u91d1\u4ee3\u7801\s+518880\s+(20\d{6}) \u4fe1\u606f\u5185\u5bb9.*?"
                      r"\u57fa\u91d1\u4efd\u989d\u51c0\u503c\(\u5355\u4f4d:\u5143\)\s*\uffe5\s*(\d+\.\d+)", pcf_text)
    if (not prior or datetime.strptime(prior[1], "%Y%m%d").date().isoformat() != as_of
            or Decimal(prior[2]) != nav or not nav.is_finite() or nav <= 0):
        raise ValueError("Product NAV does not match the PCF's prior-session date/value; no future PCF-date promotion")
    headers = [i for i, line in enumerate(lines) if [cell.strip() for cell in line.split("|")] == HEADERS]
    if len(headers) != 1 or "* \u5386\u53f2\u5206\u7ea2" not in lines[:headers[0]]:
        raise ValueError("Missing named lifetime distribution table with explicit types")
    closing = [i for i in range(headers[0] + 1, len(lines))
               if lines[i].startswith("###") and "\u5206\u7ea2\u516c\u544a" in lines[i]]
    if len(closing) != 1:
        raise ValueError("No unambiguous end of the public historical distribution table")
    table = [line for line in lines[headers[0] + 1:closing[0]] if line]
    if (not table or not re.fullmatch(r"[- |]+", table[0])
            or [[cell.strip() for cell in line.split("|")] for line in table[1:]] != [SPLIT]):
        raise ValueError("Empty, filtered or changed history cannot establish no cash; review new distribution types/rows")
    return {"code": CODE, "name": NAME, "as_of": as_of, "nav_rmb": str(nav), "cash_records": [],
            "observed_split_records": [{"date": "2013-07-24", "ratio": "0.37761358"}],
            "cash_history_basis": "closed_unfiltered_public_lifetime_table_contains_only_the_known_2013_split",
            "complete_cash_history": True, "share_actions_complete": False}


def collect(page, pcf, out=None):
    contents = {"518880_product.web.txt": (PAGE, Path(page).read_bytes()),
                "518880_pcf.web.txt": (PCF, Path(pcf).read_bytes())}
    parsed = parse(*(raw for _, raw in contents.values()))
    retrieved = datetime.now().astimezone()
    if parsed["as_of"] > retrieved.astimezone(MARKET_TZ).date().isoformat():
        raise ValueError("Future issuer NAV date cannot establish current historical coverage")
    out = Path(out) if out else DATA_ROOT / "research/fund_issuer_history" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    receipts = {}
    for name, (url, raw) in contents.items():
        (out / name).write_bytes(raw)
        receipts[name] = {"file": name, "source_url": url, "sha256": sha256(raw).hexdigest()}
    state = {"status": "completed_public_issuer_distribution_table", "kind": KIND, **parsed,
             "receipts": receipts, "retrieved_at": retrieved.isoformat(),
             "acquisition_format": "web_tool_full_page_text_not_direct_html", "execution_admitted": False,
             "limitations": ["Coverage is the named unfiltered issuer table, not an independently complete actions feed",
                             "2013 split is outside the research span and is NOT a cash dividend",
                             "No assertion of complete share actions, account readiness or native client verification",
                             "Any added/changed table record requires renewed parsing and source review"]}
    write_json(out / "result.json", state)
    return out, state


def load(folder):
    folder = Path(folder)
    state = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if state.get("status") != "completed_public_issuer_distribution_table" or state.get("kind") != KIND:
        raise ValueError("Expected the registered archived Huaan distribution table")
    urls = {"518880_product.web.txt": PAGE, "518880_pcf.web.txt": PCF}
    if set(state["receipts"]) != set(urls):
        raise ValueError("Unexpected Huaan archive files")
    raw = []
    for name, url in urls.items():
        receipt = state["receipts"][name]
        if receipt["file"] != name or receipt["source_url"] != url:
            raise ValueError("Huaan source identity changed")
        raw.append(archived(folder, receipt))
    parsed = parse(*raw)
    retrieved = datetime.fromisoformat(state["retrieved_at"])
    if retrieved.tzinfo is None or parsed["as_of"] > retrieved.astimezone(MARKET_TZ).date().isoformat():
        raise ValueError("Issuer NAV date is later than its actual acquisition date")
    if any(state.get(key) != value for key, value in parsed.items()):
        raise ValueError("Huaan history differs from its archived primary text")
    return state


def period_evidence(folder, events, first, last, code=CODE):
    state = load(folder)
    if (code != CODE or not "2023-01-03" <= first <= last <= state["as_of"]
            or date.fromisoformat(first).isoformat() != first or date.fromisoformat(last).isoformat() != last
            or "instrument_code" not in events or not events.instrument_code.eq(code).all()):
        raise ValueError("Huaan public history does not establish this code/date coverage")
    if not events.empty:
        raise ValueError("Huaan public no-cash table conflicts with reviewed PDF cash events")
    receipt = state["receipts"]["518880_product.web.txt"]
    return {"instrument_code": code, "start": first, "end": last, "source_kind": KIND,
            "source_file": receipt["file"], "source_sha256": receipt["sha256"], "source_url": PAGE,
            "as_of": state["as_of"], "cash_history_basis": state["cash_history_basis"], "events": [],
            "share_actions_complete": False, "source_format": state["acquisition_format"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-extraction", type=Path, required=True)
    parser.add_argument("--pcf-extraction", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, state = collect(args.page_extraction, args.pcf_extraction, args.out)
    print(json.dumps({"folder": str(folder), "code": CODE, "as_of": state["as_of"],
                      "cash_records": len(state["cash_records"]), "share_actions_complete": False,
                      "execution_admitted": False}), flush=True)
    return 0
