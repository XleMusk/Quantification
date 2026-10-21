"""Collect issuer notices for missing action terms; never auto-admit an account feed."""
import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests

from ..market_dataset import write_frame, write_json
from ..market_snapshot import RESERVE_CORRECTION_FIELDS, official_distribution_notice_date
from ..settings import DATA_ROOT
from .adjusted_prices import checked_file


INDEX = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
DAY = r"([0-9]{4})\u5e74([0-9]{1,2})\u6708([0-9]{1,2})\u65e5"
SLASH_DAY = r"([0-9]{4})/([0-9]{1,2})/([0-9]{1,2})"
IMPLEMENTATION_TITLE = re.compile(
    r"(?:\u6743\u76ca\u5206[\u6d3e\u914d]|\u5229\u6da6\u5206[\u6d3e\u914d]|\u5206\u7ea2\u6d3e\u606f)\s*"
    r"(?:(?:[\uff08(]\s*A\s*\u80a1\s*[\uff09)]|A\s*\u80a1)\s*)?\u5b9e\u65bd\u516c\u544a")
PDF_READER = "from pypdf import PdfReader; import json,sys; print(json.dumps({'pages':[p.extract_text() for p in PdfReader(sys.argv[1]).pages]},ensure_ascii=True))"
CASH_ONLY_STATEMENT = (r"(?:^|[\n\u3002\uff1b;])(?:[0-9]+[\u3001.]?)?(?:\u672c)?\u516c\u53f8\u672c\u6b21\u4ec5\u8fdb\u884c\u73b0\u91d1\u7ea2\u5229\u5206\u914d[\uff0c,]"
                       r"\u65e0\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d[\uff0c,\u3002]")

# Exact issuer archive, not a general mirror allowlist. Its later upload date
# is retained: this source can repair historical accounting, never prove PIT.
ISSUER_IMPLEMENTATION_NOTICES = {
    "https://www.hcpect.com/profile/upload/2026/07/28/%E6%83%A0%E5%9F%8E%E7%8E%AF%E4%BF%9D%EF%BC%9A2025%E5%B9%B4%E5%B9%B4%E5%BA%A6%E6%9D%83%E7%9B%8A%E5%88%86%E6%B4%BE%E5%AE%9E%E6%96%BD%E5%85%AC%E5%91%8A_20260728103136A025.pdf": {
        "symbol": "300779.SZSE", "source_date": "2026-06-04",
        "issuer_page_url": "https://www.hcpect.com/investor/notice_detail/1302.html",
        "issuer_archive_upload_date": "2026-07-28",
    }
}


def cash_only_statements(pages):
    text = re.sub(r"[^\S\n]+", "", "\n".join(pages))
    return re.findall(CASH_ONLY_STATEMENT, text)


class MissingShareTerms(ValueError):
    pass


class NoticeTitle(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(value)
        self.close()

    def handle_data(self, value):
        self.parts.append(value)

    @property
    def text(self):
        return "".join(self.parts)


def is_implementation_title(value):
    text = NoticeTitle(value).text
    return bool(IMPLEMENTATION_TITLE.search(text)) and not re.search(r"H\s*\u80a1", text, re.IGNORECASE)


def notice_url(relative):
    if not isinstance(relative, str) or not re.fullmatch(r"finalpage/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]+\.[Pp][Dd][Ff]", relative):
        raise ValueError("Expected the public CNinfo dated PDF route")
    return "https://static.cninfo.com.cn/" + relative


class IssuerPage(NoticeTitle):
    def __init__(self, value):
        self.links = []
        super().__init__(value)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            link = dict(attrs).get("href")
            if link:
                self.links.append(link)


def primary_notice_date(url):
    if url in ISSUER_IMPLEMENTATION_NOTICES:
        return ISSUER_IMPLEMENTATION_NOTICES[url]["source_date"]
    return official_distribution_notice_date(url)


def validate_primary_notice_source(entry, root):
    url = entry["source_url"]
    if url not in ISSUER_IMPLEMENTATION_NOTICES:
        if entry["source_date"] != official_distribution_notice_date(url):
            raise ValueError("Source date and official URL disagree")
        return
    expected = ISSUER_IMPLEMENTATION_NOTICES[url]
    if (any(entry.get(key) != value for key, value in expected.items())
            or entry.get("source_channel") != "issuer_linked_archive"
            or entry.get("point_in_time_availability_verified") is not False):
        raise ValueError("Exact issuer archive context required; no PIT admission")
    raw = checked_file(Path(root), entry["issuer_page_file"], entry["issuer_page_sha256"]).read_bytes()
    page = IssuerPage(raw.decode("utf-8"))
    target = urlparse(url)
    links = [urlparse(urljoin(expected["issuer_page_url"], link)) for link in page.links]
    if (expected["source_date"] not in page.text or not any(
            link.scheme in {"http", "https"} and link.netloc == target.netloc
            and unquote(link.path) == unquote(target.path) and not link.query and not link.fragment
            for link in links)):
        raise ValueError("Archived issuer page must explicitly link this notice and display its date")


def explicit_cash_terms(pages, action):
    if not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
        raise ValueError("Complete text-bearing PDF pages required; scanned/OCR pages need review")
    text = re.sub(r"\s+", "", "".join(pages))
    code = action["symbol"].split(".")[0]
    if not re.search(r"(?:\u8bc1\u5238|\u80a1\u7968)\u4ee3\u7801[:\uff1a]?" + code + r"(?![0-9])", text):
        raise ValueError("Issuer PDF security code differs from the source event")
    joint_no_shares = re.search(
        r"\u4e0d(?:\u8fdb\u884c|\u5b9e\u65bd)(?:\u8d44\u672c)?\u516c\u79ef(?:\u91d1)?\u8f6c\u589e\u80a1\u672c(?:\u548c|\u53ca|\u3001)(?:\u9001|\u6d3e\u9001)\u7ea2\u80a1"
        r"|\u4e0d(?:\u8fdb\u884c|\u5b9e\u65bd)(?:\u9001\u80a1|\u9001\u7ea2\u80a1)(?:\u548c|\u53ca|\u3001)(?:\u8d44\u672c)?\u516c\u79ef(?:\u91d1)?\u8f6c\u589e\u80a1\u672c", text)
    joint_no_shares = joint_no_shares or cash_only_statements(pages)
    joint_no_shares = joint_no_shares or re.search(
        r"\u672c\u6b21\u4ec5\u8fdb\u884c\u73b0\u91d1\u7ea2\u5229\u5206\u914d[\uff0c,]"
        r"\u65e0\u914d\u80a1\u3001\u9001\u80a1\u548c\u8f6c\u589e\u5206\u914d[\uff0c,\u3002]", text)
    no_bonus = re.search(r"\u4e0d(?:\u9001|\u5206)\u7ea2\u80a1|\u4e0d\u9001\u80a1|\u6bcf10\u80a1(?:\u6d3e)?\u90010(?:\.0+)?\u80a1", text)
    if not joint_no_shares and (not no_bonus or not re.search(
            r"\u4e0d(?:\u8fdb\u884c|\u4ee5|\u5b9e\u65bd)(?:\u8d44\u672c)?\u516c\u79ef(?:\u91d1)?\u8f6c\u589e(?:\u80a1\u672c|(?=[\u3002\uff1b;]|$))"
            r"|\u4e0d(?:\u8fdb\u884c|\u5b9e\u65bd)?\u8f6c\u589e(?:\u80a1\u672c|(?=[\u3002\uff1b;]|$))", text)):
        raise MissingShareTerms("Issuer must explicitly state no bonus stock AND no reserve transfer")
    share_clauses = re.findall(r"(?:\u6bcf)?10(?:\u80a1)?[^\u3002]{0,20}?(?:\u9001|\u8f6c\u589e|\u8f6c)" + NUMBER, text)
    if any(Decimal(value) > 0 for value in share_clauses):
        raise ValueError("A positive share-plan clause conflicts with cash-only completion")
    # Annual totals and previously paid instalments are not this implementation's cash.
    sections = re.findall(
        r"[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+[\u3001\uff0e.]"
        r"(?:\u672c\u6b21\u5b9e\u65bd\u7684(?:\u6743\u76ca\u5206\u6d3e|\u5229\u6da6\u5206\u914d)\u65b9\u6848|\u6743\u76ca\u5206\u6d3e\u65b9\u6848)"
        r"(.*?)(?=[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+[\u3001\uff0e.]|$)", text)
    if len(sections) > 1:
        raise ValueError("Multiple implementation-plan sections require review; no selecting among revisions")
    cash_text = sections[0] if sections else text
    cash = re.findall(r"\u6bcf10\u80a1(?:\u6d3e\u53d1|\u6d3e\u9001|\u6d3e)(?:\u73b0\u91d1\u80a1\u5229|\u73b0\u91d1\u7ea2\u5229|\u73b0\u91d1|\u80a1\u5229|\u7ea2\u5229|\u4eba\u6c11\u5e01)*" + NUMBER
                      + r"\u5143(?:\u4eba\u6c11\u5e01(?:\u73b0\u91d1)?)?[\uff0c,]?[\uff08(]\u542b\u7a0e", cash_text)
    amounts = {Decimal(value) / 10 for value in cash}
    per_share = re.findall(r"\u6bcf\u80a1(?:\u6d3e\u53d1|\u6d3e\u9001|\u6d3e)(?:\u4eba\u6c11\u5e01)?(?:\u73b0\u91d1\u7ea2\u5229|\u73b0\u91d1\u80a1\u5229)"
                           + r"(?:\u4eba\u6c11\u5e01)?" + NUMBER + r"\u5143[\uff0c,]?[\uff08(]\u542b\u7a0e", cash_text)
    amounts.update(Decimal(value) for value in per_share)
    if (len(amounts) != 1 or next(iter(amounts)) <= 0 or action["dividCashPsBeforeTax"] == ""
            or next(iter(amounts)) != Decimal(action["dividCashPsBeforeTax"])):
        raise ValueError("Unique explicit gross cash must equal the original amount; no rounded/proposed-total substitution")
    for field in ("dividStocksPs", "dividReserveToStockPs"):
        if action[field] != "" and Decimal(action[field]) != 0:
            raise ValueError("Explicit no-share notice conflicts with an existing reported share amount")
    dates = {}
    # Require the standard ordered A-share table, not arbitrary dates nearby.
    table_pattern = (r"\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5"
                     r"\u9664\u6743[\uff08(]\u606f[\uff09)]\u65e5\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5"
                     r"[A\uff21]\u80a1" + SLASH_DAY + r"[\uff0d\u2014-]" + SLASH_DAY + SLASH_DAY)
    table_dates = [tuple(date(*map(int, row[i:i + 3])).isoformat() for i in (0, 3, 6))
                   for row in re.findall(table_pattern, text)]
    if action["symbol"].endswith(".SSE") and not action.get("dividStockMarketDate"):
        cash_table = (r"\u80a1\u6743\u767b\u8bb0\u65e5\u9664\u6743[\uff08(]\u606f[\uff09)]\u65e5"
                      r"\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5" + SLASH_DAY * 3)
        table_dates.extend(tuple(date(*map(int, row[i:i + 3])).isoformat() for i in (0, 3, 6))
                           for row in re.findall(cash_table, text))
    for field, label in (("dividRegistDate", r"\u80a1\u6743\u767b\u8bb0\u65e5"),
                         ("dividOperateDate", r"(?:\u9664\u6743\u9664\u606f\u65e5|\u9664\u6743\u65e5[\uff08(]\u9664\u606f\u65e5[\uff09)]|\u9664\u606f\u65e5)")):
        observed = {date(*map(int, match)).isoformat() for match in re.findall(label + r"(?:\u4e3a|\u662f)?[:\uff1a]?" + DAY, text)}
        observed.update(row[0 if field == "dividRegistDate" else 1] for row in table_dates)
        if observed != {action[field]}:
            raise ValueError("Unique issuer record/ex date must match the pinned event")
        dates[field] = next(iter(observed))
    paid = {date(*map(int, match)).isoformat() for match in re.findall(
        r"\u73b0\u91d1(?:\u7ea2\u5229|\u80a1\u5229)(?:\u5c06)?\u4e8e" + DAY
        + r"[^\u3002]{0,120}(?:\u8d44\u91d1\u8d26\u6237|\u76f4\u63a5\u5212\u5165\u5176\u8d44\u91d1\u8d26(?=\u3002))", text)}
    paid.update(row[2] for row in table_dates)
    if paid != {action["dividPayDate"]} or not dates["dividRegistDate"] < dates["dividOperateDate"] <= action["dividPayDate"]:
        raise ValueError("Unique issuer payment date and event chronology required")
    return {"dividCashPsBeforeTax": str(next(iter(amounts))), "dividStocksPs": "0", "dividReserveToStockPs": "0"}


def fetch(session, method, url, **kwargs):
    with session.request(method, url, timeout=(5, 25), stream=True, **kwargs) as response:
        response.raise_for_status()
        if urlparse(response.url).hostname != urlparse(url).hostname:
            raise ValueError("Unexpected public-source redirect")
        parts, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 20_000_000:
                raise ValueError("Public notice response exceeds20MB")
            parts.append(chunk)
    return b"".join(parts)


def distribution_period(value):
    text = re.sub(r"\s+", "", NoticeTitle(value).text)
    matches = {(year, "interim" if kind in {"\u534a\u5e74\u5ea6", "\u4e2d\u671f"} else "annual")
               for year, kind in re.findall(
                   r"(20[0-9]{2})(?:\u5e74)?(\u5e74\u5ea6|\u534a\u5e74\u5ea6|\u4e2d\u671f)"
                   r"(?:\u6743\u76ca\u5206[\u6d3e\u914d]|\u5229\u6da6\u5206[\u6d3e\u914d])", text)}
    if len(matches) != 1:
        raise ValueError("Unique explicit annual/interim distribution period required")
    return next(iter(matches))


def final_distribution_period(pages):
    if not pages or not isinstance(pages[0], str) or not pages[0].strip():
        raise ValueError("Text-bearing implementation heading required")
    header = re.split(r"\u672c\u516c\u53f8|\u7279\u522b\u63d0\u793a", pages[0], maxsplit=1)[0]
    return distribution_period(header)


def companion_notice(page, code, period=None):
    if page.get("hasMore") or not isinstance(page.get("announcements"), list):
        raise ValueError("Companion metadata page incomplete")
    related = [(notice, NoticeTitle(notice.get("announcementTitle", "")).text)
               for notice in page["announcements"] if notice.get("secCode") == code]
    related = [(notice, title) for notice, title in related
               if re.search(r"\u5229\u6da6\u5206\u914d(?:\u9884\u6848|\u65b9\u6848)", title)]
    if period is not None:
        related = [(notice, title) for notice, title in related if distribution_period(title) == period]
    for _, title in related:
        convertible = re.search(r"\u6839\u636e.*\u5229\u6da6\u5206\u914d\u65b9\u6848\u8c03\u6574.*\u8f6c\u80a1\u4ef7\u683c", title)
        if not convertible and re.search(r"\u8c03\u6574|\u53d8\u66f4|\u4fee\u8ba2|\u66f4\u6b63|\u53d6\u6d88", title):
            raise ValueError("Companion distribution revision present; no selecting the original plan")
    notices = [notice for notice, title in related if re.search(
        r"\u5229\u6da6\u5206\u914d(?:\u9884\u6848|\u65b9\u6848)(?:\u7684)?\u516c\u544a$", title)]
    if len(notices) != 1:
        raise ValueError("Unique companion proposal required; no picking among revisions")
    return notices[0]


def acquire_companion(session, pdf_python, root, folder, action, final_pages, entry, org_id, *, metadata_from=None):
    """Acquire one unique primary proposal; approval/term checks remain in the linker."""
    source_date = date.fromisoformat(entry["source_date"])
    params = {"pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
              "stock": f"{action['symbol'].split('.')[0]},{org_id}",
              "seDate": f"{source_date.year}-01-01~{source_date}",
              "searchkey": "\u5229\u6da6\u5206\u914d", "secid": "", "category": "", "trade": "",
              "sortName": "", "sortType": "", "isHLtitle": "true"}
    entry.update(companion_acquisition_attempted=True)
    if metadata_from is None:
        entry["plan_query_params"] = params
        raw = fetch(session, "POST", QUERY, data=params)
    else:
        raw = Path(metadata_from).read_bytes()
        entry["plan_metadata_reused_from"] = str(Path(metadata_from).resolve())
    metadata = folder / "plan_metadata.json"
    metadata.write_bytes(raw)
    entry.update(plan_metadata_file=metadata.relative_to(root).as_posix(),
                 plan_metadata_sha256=sha256(raw).hexdigest())
    page = json.loads(raw)
    code = action["symbol"].split(".")[0]
    period = final_distribution_period(final_pages)
    entry["companion_distribution_period"] = {"year": period[0], "kind": period[1]}
    notice = companion_notice(page, code, period)
    url = notice_url(notice["adjunctUrl"])
    plan_date = url.split("/")[-2]
    if not f"{source_date.year}-01-01" <= plan_date <= entry["source_date"]:
        raise ValueError("Companion publication outside the pre-final query window")
    raw_pdf = fetch(session, "GET", url)
    if not raw_pdf.startswith(b"%PDF-"):
        raise ValueError("Companion response is not a PDF")
    pdf = folder / "plan_notice.pdf"
    pdf.write_bytes(raw_pdf)
    entry.update(plan_source_file=pdf.relative_to(root).as_posix(),
                 plan_source_sha256=sha256(raw_pdf).hexdigest(), plan_source_date=plan_date,
                 plan_source_url=url, plan_announcement_title=notice["announcementTitle"])
    parsed = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(pdf)],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
    pages = json.loads(parsed.stdout)["pages"]
    text = folder / "plan_text.json"
    write_json(text, {"pages": pages})
    entry.update(plan_text_file=text.relative_to(root).as_posix(),
                 plan_text_sha256=sha256(text.read_bytes()).hexdigest(),
                 plan_source_pages=list(range(1, len(pages) + 1)))
    entry.update(explicit_plan_link(final_pages, pages, action, entry["source_date"], plan_date))


def archived_stock_index(index_root):
    root = Path(index_root).resolve()
    state = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if state.get("status") != "batch_completed" or state.get("execution_admitted") is not False:
        raise ValueError("Stock-index reuse requires a completed unadmitted disclosure batch")
    sources = {(row["stock_index_file"], row["stock_index_sha256"]) for row in state["records"]
               if "stock_index_file" in row}
    if len(sources) != 1:
        raise ValueError("One archived public stock-index receipt required")
    name, digest = next(iter(sources))
    raw = checked_file(root, name, digest).read_bytes()
    catalog = json.loads(raw)["stockList"]
    if (not catalog or any(not re.fullmatch(r"[0-9]{6}", row["code"]) or not row["orgId"] for row in catalog)
            or len({row["code"] for row in catalog}) != len(catalog)):
        raise ValueError("Distinct public security-code/org-ID discovery catalog required")
    return raw, {"file": name, "sha256": digest, "source_root": str(root), "url": INDEX,
                 "basis": "Archived issuer discovery only; query security code, unique dated notice and PDF identity still checked"}


def prepare(event_view, root=None, reuse_root=None, *, priority_events=(), index_root=None):
    event_view = Path(event_view).resolve()
    result = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    path = checked_file(event_view, "canonical_actions.parquet", result["files"]["canonical_actions.parquet"])
    if result["status"] != "research_action_events_projected":
        raise ValueError("Pinned canonical action events required")
    actions = pd.read_parquet(path)
    fields = ["dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
    priority_events = [tuple(key) for key in priority_events]
    if len(set(priority_events)) != len(priority_events):
        raise ValueError("Unique explicit priority events required")
    selected = actions[fields].eq("").any(axis=1)
    for key in priority_events:
        if len(key) != 2 or date.fromisoformat(key[1]).isoformat() != key[1]:
            raise ValueError("Canonical priority event symbol/date required")
        matched = actions.symbol.eq(key[0]) & actions.dividOperateDate.eq(key[1])
        if int(matched.sum()) != 1:
            raise ValueError("Priority must identify one pinned canonical event")
        selected |= matched
    targets = actions.loc[selected].sort_values(["dividOperateDate", "symbol"]).reset_index(drop=True)
    root = Path(root) if root else DATA_ROOT / "research/stock_action_disclosures" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(path, root / "input_actions.parquet")
    shutil.copy2(event_view / "result.json", root / "input_events.json")
    shutil.copy2(Path(__file__), root / "implementation.py")
    write_frame(root / "targets.parquet", targets)
    plan = {"event_view": str(event_view), "input_actions_sha256": result["files"]["canonical_actions.parquet"],
            "targets_sha256": sha256((root / "targets.parquet").read_bytes()).hexdigest(),
            "implementation_sha256": sha256((root / "implementation.py").read_bytes()).hexdigest(),
            "source_events": len(actions), "target_events": len(targets), "start": result["start"], "end": result["end"],
            "purpose": "All blank amount events plus explicitly prioritized canonical ledger blockers; no source term mutation",
            "explicit_priority_events": priority_events,
            "execution_admitted": False, "automatic_promotion": False}
    records = []
    if reuse_root is not None:
        reuse_root = Path(reuse_root).resolve()
        old_plan = json.loads((reuse_root / "plan.json").read_text(encoding="utf-8"))
        old_state = json.loads((reuse_root / "result.json").read_text(encoding="utf-8"))
        if old_state["status"] != "batch_completed":
            raise ValueError("Reuse requires a terminal completed disclosure batch")
        checked_file(reuse_root, "input_actions.parquet", old_plan["input_actions_sha256"])
        checked_file(reuse_root, "implementation.py", old_plan["implementation_sha256"])
        by_key = {(row["symbol"], row["dividOperateDate"]): row for row in targets.to_dict("records")}
        for row in old_state["records"]:
            key = row["symbol"], row["date"]
            if key not in by_key:
                continue
            if any(by_key[key][field] != row["expected_provider_terms"][field] for field in RESERVE_CORRECTION_FIELDS):
                raise ValueError("Archived disclosure source terms changed")
            for stem in ("metadata", "source", "text", "stock_index", "plan_metadata", "plan_source", "plan_text"):
                if stem + "_file" in row:
                    source = checked_file(reuse_root, row[stem + "_file"], row[stem + "_sha256"])
                    target = root / row[stem + "_file"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            records.append({**row, "reused_from": str(reuse_root)})
        for name in ("plan.json", "result.json"):
            shutil.copy2(reuse_root / name, root / ("reused_" + name))
        plan.update(reused_root=str(reuse_root), reused_events=len(records),
                    reused_result_sha256=sha256((root / "reused_result.json").read_bytes()).hexdigest())
    if index_root is not None:
        raw, receipt = archived_stock_index(index_root)
        target = root / receipt["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        plan["archived_stock_index"] = receipt
    write_json(root / "plan.json", plan)
    write_json(root / "result.json", {"status": "prepared", "target_events": len(targets), "records": records})
    return root


def collect(root, pdf_python, max_events=20, priority_events=(), *, with_plans=False):
    root, pdf_python = Path(root), Path(pdf_python)
    if not pdf_python.is_file() or not 1 <= max_events <= 64:
        raise ValueError("Existing PDF interpreter and bounded1..64 event batch required")
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    checked_file(root, "input_actions.parquet", plan["input_actions_sha256"])
    checked_file(root, "implementation.py", plan["implementation_sha256"])
    if sha256(Path(__file__).read_bytes()).hexdigest() != plan["implementation_sha256"]:
        raise ValueError("Collector changed; retain this root and prepare a new version")
    targets = pd.read_parquet(checked_file(root, "targets.parquet", plan["targets_sha256"])).to_dict("records")
    state = json.loads((root / "result.json").read_text(encoding="utf-8"))
    done = {(row["symbol"], row["date"]) for row in state["records"]}
    pending = [row for row in targets if (row["symbol"], row["dividOperateDate"]) not in done]
    priority = {tuple(key): i for i, key in enumerate(priority_events)}
    if not set(priority) <= {(row["symbol"], row["dividOperateDate"]) for row in targets}:
        raise ValueError("Priority events must be prepared targets in this pinned action view")
    pending.sort(key=lambda row: priority.get((row["symbol"], row["dividOperateDate"]), len(priority)))
    if priority:
        state["priority_events"] = list(priority)
    state.update(status="collecting", execution_admitted=False, automatic_promotion=False,
                 companion_acquisition_enabled=with_plans)
    write_json(root / "result.json", state)
    with requests.Session() as session:
        if plan.get("archived_stock_index"):
            receipt = plan["archived_stock_index"]
            index = checked_file(root, receipt["file"], receipt["sha256"]).read_bytes()
        else:
            index = fetch(session, "GET", INDEX)
        catalog = json.loads(index)["stockList"]
        orgs = {row["code"]: row["orgId"] for row in catalog}
        index_name = "stock_index_" + sha256(index).hexdigest()[:16] + ".json"
        (root / index_name).write_bytes(index)
        for number, action in enumerate(pending[:max_events], 1):
            symbol, ex = action["symbol"], action["dividOperateDate"]
            code = symbol.split(".")[0]
            entry = {"symbol": symbol, "date": ex, "retrieved_at": datetime.now().astimezone().isoformat(),
                     "expected_provider_terms": {field: action[field] for field in RESERVE_CORRECTION_FIELDS},
                     "stock_index_file": index_name, "stock_index_sha256": sha256(index).hexdigest()}
            folder = root / symbol / ex
            folder.mkdir(parents=True, exist_ok=False)
            try:
                start = date.fromisoformat(action["dividPlanDate"]) - timedelta(days=1)
                end = date.fromisoformat(action["dividRegistDate"])
                if code not in orgs or start > end or (end - start).days > 40:
                    raise ValueError("Unsupported or missing reviewed issuer index/date window")
                params = {"pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
                          "stock": f"{code},{orgs[code]}", "seDate": f"{start}~{end}", "searchkey": "",
                          "secid": "", "category": "", "trade": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
                raw = fetch(session, "POST", QUERY, data=params)
                (folder / "metadata.json").write_bytes(raw)
                entry.update(query_params=params, metadata_file=(folder / "metadata.json").relative_to(root).as_posix(),
                             metadata_sha256=sha256(raw).hexdigest())
                page = json.loads(raw)
                if page.get("hasMore") or not isinstance(page.get("announcements"), list):
                    raise ValueError("Incomplete or invalid public announcement page")
                notices = [row for row in page["announcements"] if row.get("secCode") == code
                           and is_implementation_title(row.get("announcementTitle", ""))]
                if len(notices) != 1:
                    raise ValueError("Unique issuer implementation notice required; no choosing among revisions")
                notice = notices[0]
                url = notice_url(notice["adjunctUrl"])
                source_day = url.split("/")[-2]
                if not str(start) <= source_day <= action["dividRegistDate"]:
                    raise ValueError("Implementation notice outside the query/pre-record window")
                raw_pdf = fetch(session, "GET", url)
                if not raw_pdf.startswith(b"%PDF-"):
                    raise ValueError("Notice response is not a PDF")
                pdf = folder / "notice.pdf"
                pdf.write_bytes(raw_pdf)
                entry.update(source_date=source_day, source_url=url, source_file=pdf.relative_to(root).as_posix(),
                             source_sha256=sha256(raw_pdf).hexdigest(), announcement_title=notice["announcementTitle"])
                parsed = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(pdf)],
                                        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
                pages = json.loads(parsed.stdout)["pages"]
                write_json(folder / "text.json", {"pages": pages})
                entry.update(extracted_pages=len(pages), text_file=(folder / "text.json").relative_to(root).as_posix(),
                             text_sha256=sha256((folder / "text.json").read_bytes()).hexdigest())
                try:
                    entry["explicit_terms"] = explicit_cash_terms(pages, action)
                    entry["status"] = "machine_extracted_explicit_cash_terms_candidate"
                except MissingShareTerms as error:
                    if not with_plans:
                        raise
                    entry["final_only_reason"] = str(error)
                    acquire_companion(session, pdf_python, root, folder, action, pages, entry, orgs[code])
            except ValueError as error:
                entry.update(status="needs_review", reason=str(error))
            except Exception as error:
                entry.update(status="source_or_reader_error", reason=f"{type(error).__name__}: {error}")
                state["records"].append(entry)
                state.update(status="stopped_source_or_reader_error")
                write_json(root / "result.json", state)
                raise
            state["records"].append(entry)
            write_json(root / "result.json", state)
            print(f"{number}/{min(max_events,len(pending))} {symbol} {ex} {entry['status']}", flush=True)
    state.update(status="batch_completed", attempted_events=len(state["records"]),
                 unattempted_events=len(targets) - len(state["records"]),
                 explicit_candidates=sum(row["status"] in {"machine_extracted_explicit_cash_terms_candidate",
                                                           "machine_linked_explicit_cash_terms_candidate"} for row in state["records"]),
                 limits=["Explicit-phrase PDF extraction, not manual review, historical-PIT availability or completeness",
                         "No blank-to-zero defaults; noncash, conflicting, ambiguous or image-only notices need review",
                         "No active supplement, provider source, model, price, account or execution change"])
    write_json(root / "result.json", state)
    return state


def reparse(root):
    """Review a new parser against archived notices without changing the collection."""
    root = Path(root).resolve()
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    original = json.loads((root / "result.json").read_text(encoding="utf-8"))
    actions = pd.read_parquet(checked_file(root, "input_actions.parquet", plan["input_actions_sha256"]))
    checked_file(root, "targets.parquet", plan["targets_sha256"])
    checked_file(root, "implementation.py", plan["implementation_sha256"])
    targets = {(row["symbol"], row["dividOperateDate"]): row for row in actions.to_dict("records")}
    folder = root / "views" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("plan.json", "result.json", "implementation.py"):
        shutil.copy2(root / name, folder / ("input_" + name))
    shutil.copy2(Path(__file__), folder / "implementation.py")
    records = []
    for original_row in original["records"]:
        row = {**original_row, "original_status": original_row["status"],
               "original_reason": original_row.get("reason", "")}
        for stem in ("metadata", "source", "text", "stock_index", "plan_metadata", "plan_source", "plan_text"):
            if stem + "_file" not in row:
                continue
            source = checked_file(root, row[stem + "_file"], row[stem + "_sha256"])
            destination = folder / row[stem + "_file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if "text_file" in row:
            pages = json.loads((folder / row["text_file"]).read_text(encoding="utf-8"))["pages"]
            if len(pages) != row["extracted_pages"]:
                raise ValueError("Archived PDF page count changed")
            row.pop("explicit_terms", None)
            row.pop("reason", None)
            try:
                action = targets[(row["symbol"], row["date"])]
                if "plan_text_file" in row:
                    companion = json.loads((folder / row["plan_text_file"]).read_text(encoding="utf-8"))["pages"]
                    row.update(explicit_plan_link(pages, companion, action, row["source_date"], row["plan_source_date"]))
                else:
                    row["explicit_terms"] = explicit_cash_terms(pages, action)
                    row["status"] = "machine_extracted_explicit_cash_terms_candidate"
            except ValueError as error:
                row.update(status="needs_review", reason=str(error))
        records.append(row)
    result = {"status": "archived_disclosures_reparsed", "source_root": str(root),
              "target_events": plan["target_events"], "attempted_events": len(records),
              "unattempted_events": plan["target_events"] - len(records), "records": records,
              "explicit_candidates": sum(row["status"] in {"machine_extracted_explicit_cash_terms_candidate",
                                                           "machine_linked_explicit_cash_terms_candidate"} for row in records),
              "execution_admitted": False, "automatic_promotion": False,
              "files": {p.relative_to(folder).as_posix(): sha256(p.read_bytes()).hexdigest()
                        for p in folder.rglob("*") if p.is_file()}}
    write_json(folder / "result.json", result)
    return folder, result


def explicit_plan_link(final_pages, plan_pages, action, final_date, plan_date):
    """A proposal supplies explicit terms only with a matching approved final plan."""
    def document(pages):
        if not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
            raise ValueError("Complete text-bearing companion/final pages required")
        text = re.sub(r"\s+", "", "".join(pages))
        code = action["symbol"].split(".")[0]
        if not re.search(r"(?:\u8bc1\u5238|\u80a1\u7968)\u4ee3\u7801[:\uff1a]?" + code + r"(?![0-9])", text):
            raise ValueError("Companion/final issuer code conflict")
        return text
    final, plan = document(final_pages), document(plan_pages)
    years = set(re.findall(r"(20[0-9]{2})\u5e74(?:\u5e74)?\u5ea6(?:\u6743\u76ca\u5206\u6d3e|\u5229\u6da6\u5206\u914d)", final))
    plan_years = set(re.findall(r"(20[0-9]{2})\u5e74(?:\u5e74)?\u5ea6\u5229\u6da6\u5206\u914d", plan))
    if len(years) != 1 or plan_years != years:
        raise ValueError("Require the same unique annual distribution period, not an interim/other-year plan")
    shareholder = {date(*map(int, row)).isoformat() for row in re.findall(
        DAY + r"[^\u3002]{0,80}\u80a1\u4e1c\u5927\u4f1a[^\u3002]{0,40}\u5ba1\u8bae\u901a\u8fc7", final)}
    board = {date(*map(int, row)).isoformat() for row in re.findall(
        DAY + r"[^\u3002]{0,80}\u8463\u4e8b\u4f1a[^\u3002]{0,100}\u5ba1\u8bae\u901a\u8fc7", plan)}
    if len(shareholder) != 1 or len(board) != 1:
        raise ValueError("Unique explicit final shareholder and companion board approval required")
    approval, approved = next(iter(board)), next(iter(shareholder))
    chronology = [approval, plan_date, approved, final_date, action["dividRegistDate"]]
    if [date.fromisoformat(day) for day in chronology] != sorted(date.fromisoformat(day) for day in chronology):
        raise ValueError("Companion publication/approval must precede final implementation and registration")
    capital_pattern = r"\u603b\u80a1\u672c([0-9,]+)\u80a1"
    capital = {int(value.replace(",", "")) for value in re.findall(capital_pattern, final)}
    plan_capital = {int(value.replace(",", "")) for value in re.findall(capital_pattern, plan)}
    if len(capital) != 1 or plan_capital != capital or next(iter(capital)) <= 0:
        raise ValueError("Require unchanged explicit final/companion share capital")
    cash_pattern = (r"\u6bcf10\u80a1(?:\u6d3e\u53d1|\u6d3e\u9001|\u6d3e)"
                    r"(?:\u73b0\u91d1\u80a1\u5229|\u73b0\u91d1\u7ea2\u5229|\u73b0\u91d1|\u80a1\u5229|\u7ea2\u5229|\u4eba\u6c11\u5e01)*"
                    + NUMBER + r"\u5143[\uff0c,]?[\uff08(]\u542b\u7a0e")
    cash = {Decimal(value) / 10 for value in re.findall(cash_pattern, plan)}
    if cash != {Decimal(action["dividCashPsBeforeTax"])}:
        raise ValueError("Companion unique explicit per10 gross cash must equal the implemented amount")
    # Both real texts are parsed together; no synthetic no-share clause is added.
    terms = explicit_cash_terms([*final_pages, *plan_pages], action)
    return {"explicit_terms": terms, "annual_period": next(iter(years)),
            "plan_approval_date": approval, "shareholder_approval_date": approved,
            "unchanged_capital": next(iter(capital)),
            "status": "machine_linked_explicit_cash_terms_candidate"}


def link_plans(root, pdf_python, max_events=20, priority_events=(), *, acquire=False):
    """Consume archived pairs; explicit acquisition fetches missing companions only."""
    root, pdf_python = Path(root).resolve(), Path(pdf_python)
    if not pdf_python.is_file() or not 1 <= max_events <= 64:
        raise ValueError("Existing PDF interpreter and bounded1..64 companion batch required")
    original = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if original["status"] != "batch_completed":
        raise ValueError("Companion linking requires a terminal original collection")
    protocol = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    actions = pd.read_parquet(checked_file(root, "input_actions.parquet", protocol["input_actions_sha256"]))
    targets = {(row["symbol"], row["dividOperateDate"]): row for row in actions.to_dict("records")}
    records = {(row["symbol"], row["date"]): row for row in original["records"]}
    selected = list(priority_events) if priority_events else [key for key, row in records.items()
                                                            if row["status"] == "needs_review"]
    if len(set(selected)) != len(selected) or not set(selected) <= set(records):
        raise ValueError("Companion events must be unique acquired final notices")
    folder = root / "views" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(Path(__file__), folder / "implementation.py")
    output = []
    for key in selected[:max_events]:
        original_row, action = records[key], targets[key]
        row = {"symbol": key[0], "date": key[1], "expected_provider_terms": original_row["expected_provider_terms"],
               "status": "needs_review"}
        target = folder / key[0] / key[1]
        target.mkdir(parents=True, exist_ok=False)
        try:
            final_pdf = checked_file(root, original_row["source_file"], original_row["source_sha256"])
            parent = final_pdf.parent
            metadata = parent / "plan_metadata.json"
            files = {"source": final_pdf, "plan_source": parent / "plan_notice.pdf", "plan_metadata": metadata}
            texts = {}
            for prefix, path in files.items():
                if not path.is_file():
                    continue
                if prefix + "_file" in original_row:
                    path = checked_file(root, original_row[prefix + "_file"], original_row[prefix + "_sha256"])
                saved = target / path.name
                shutil.copy2(path, saved)
                row.update({prefix + "_file": saved.relative_to(folder).as_posix(),
                            prefix + "_sha256": sha256(saved.read_bytes()).hexdigest()})
                if prefix != "plan_metadata":
                    if not saved.read_bytes().startswith(b"%PDF-"):
                        raise ValueError("Companion/final file is not a PDF")
                    parsed = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(saved)],
                                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
                    texts[prefix] = json.loads(parsed.stdout)["pages"]
            row.update(source_date=original_row["source_date"], source_url=original_row["source_url"],
                       source_physical_pages=list(range(1, len(texts["source"]) + 1)))
            if "plan_source" not in texts:
                if not acquire:
                    raise ValueError("Companion primary metadata/PDF not acquired; no missing-term default")
                stock = original_row.get("query_params", {}).get("stock", "").split(",")
                if len(stock) != 2 or stock[0] != key[0].split(".")[0] or not stock[1]:
                    raise ValueError("Archived issuer query identity required for companion acquisition")
                with requests.Session() as session:
                    acquire_companion(session, pdf_python, folder, target, action, texts["source"], row, stock[1],
                                      metadata_from=metadata if metadata.is_file() else None)
                texts["plan_source"] = json.loads((folder / row["plan_text_file"]).read_text(encoding="utf-8"))["pages"]
            else:
                if not metadata.is_file():
                    raise ValueError("Companion primary metadata not acquired")
                period = final_distribution_period(texts["source"])
                row["companion_distribution_period"] = {"year": period[0], "kind": period[1]}
                notice = companion_notice(json.loads(metadata.read_text(encoding="utf-8")), key[0].split(".")[0], period)
                url = notice_url(notice["adjunctUrl"])
                row.update(explicit_plan_link(texts["source"], texts["plan_source"], action,
                                              original_row["source_date"], url.split("/")[-2]),
                           plan_source_date=url.split("/")[-2], plan_source_url=url,
                           plan_source_pages=list(range(1, len(texts["plan_source"]) + 1)))
            write_json(target / "texts.json", texts)
        except ValueError as error:
            row.update(status="needs_review", reason=str(error))
        except Exception as error:
            row.update(status="source_or_reader_error", reason=f"{type(error).__name__}: {error}")
            output.append(row)
            write_json(folder / "result.json", {"status": "stopped_companion_reader_error", "records": output})
            raise
        output.append(row)
        print(f"{key[0]} {key[1]} {row['status']}", flush=True)
    result = {"status": "acquired_companion_plans_linked" if acquire else "archived_companion_plans_linked",
              "network_acquisition_enabled": acquire, "source_root": str(root), "records": output,
              "attempted_events": len(output), "unattempted_selected_events": max(0, len(selected) - max_events),
              "explicit_candidates": sum(row["status"] == "machine_linked_explicit_cash_terms_candidate" for row in output),
              "execution_admitted": False, "automatic_promotion": False,
              "limitations": ["Machine text/metadata association is a candidate, not manual source/page verification",
                              "Only unique unchanged annual final/approved-plan pairs supported; no inferred zeros",
                              "No active supplement, stock exclusion, account or broker change"]}
    write_json(folder / "result.json", result)
    return folder, result


def collect_direct(event_view, notices, pdf_python):
    """Acquire explicit dated primary PDFs without repeating a failed search query."""
    event_view, pdf_python = Path(event_view).resolve(), Path(pdf_python)
    seeds = json.loads(Path(notices).read_text(encoding="utf-8"))
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 20 or not pdf_python.is_file()
            or any(set(row) != {"symbol", "ex_date", "url"} for row in seeds)):
        raise ValueError("One to20 explicit source notices and a PDF reader required")
    keys = [(row["symbol"], row["ex_date"]) for row in seeds]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate direct-notice events")
    manifest = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    if manifest["status"] != "research_action_events_projected" or manifest["execution_admitted"]:
        raise ValueError("Pinned unadmitted event view required")
    action_path = checked_file(event_view, "canonical_actions.parquet",
                               manifest["files"]["canonical_actions.parquet"])
    actions = pd.read_parquet(action_path).fillna("")
    selected = []
    for seed in seeds:
        url = seed["url"]
        day = primary_notice_date(url)
        issuer = ISSUER_IMPLEMENTATION_NOTICES.get(url)
        if issuer is not None and issuer["symbol"] != seed["symbol"]:
            raise ValueError("Issuer archive belongs to a different security")
        date.fromisoformat(seed["ex_date"])
        matches = actions.loc[actions.symbol.eq(seed["symbol"]) & actions.dividOperateDate.eq(seed["ex_date"])]
        if len(matches) != 1 or not day <= matches.iloc[0].dividRegistDate < seed["ex_date"]:
            raise ValueError("One matching event and pre-registration notice required")
        selected.append((seed, matches.iloc[0].to_dict()))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/stock_action_direct_notices" / identifier
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "request.json", {"event_view": str(event_view), "notices": seeds})
    shutil.copy2(action_path, root / "input_actions.parquet")
    shutil.copy2(event_view / "result.json", root / "input_events.json")
    write_json(root / "plan.json", {"event_view": str(event_view),
               "input_actions_sha256": manifest["files"]["canonical_actions.parquet"],
               "explicit_priority_events": keys, "target_events": len(keys),
               "execution_admitted": False, "automatic_promotion": False})
    state = {"status": "acquiring_direct_notices", "event_view": str(event_view), "records": [],
             "execution_admitted": False, "automatic_promotion": False, "search_query_retried": False}
    write_json(root / "result.json", state)
    with requests.Session() as session:
        for seed, action in selected:
            target = root / seed["symbol"] / seed["ex_date"]
            target.mkdir(parents=True)
            row = {"symbol": seed["symbol"], "date": seed["ex_date"], "dividOperateDate": seed["ex_date"],
                   "source_url": seed["url"], "source_date": primary_notice_date(seed["url"]),
                   "expected_provider_terms": {field: action[field] for field in RESERVE_CORRECTION_FIELDS}}
            try:
                issuer = ISSUER_IMPLEMENTATION_NOTICES.get(seed["url"])
                if issuer is not None:
                    page_raw = fetch(session, "GET", issuer["issuer_page_url"])
                    page_file = target / "issuer_page.html"
                    page_file.write_bytes(page_raw)
                    row.update(**issuer, source_channel="issuer_linked_archive",
                               issuer_page_file=page_file.relative_to(root).as_posix(),
                               issuer_page_sha256=sha256(page_raw).hexdigest(),
                               point_in_time_availability_verified=False)
                    validate_primary_notice_source(row, root)
                raw = fetch(session, "GET", seed["url"])
                if not raw.startswith(b"%PDF-"):
                    raise ValueError("Direct notice returned non-PDF content")
                pdf = target / "notice.pdf"
                pdf.write_bytes(raw)
                row.update(source_file=pdf.relative_to(root).as_posix(), source_sha256=sha256(raw).hexdigest())
                parsed = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(pdf)],
                                        check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
                pages = json.loads(parsed.stdout)["pages"]
                write_json(target / "text.json", {"pages": pages})
                row.update(text_file=(target / "text.json").relative_to(root).as_posix(),
                           text_sha256=sha256((target / "text.json").read_bytes()).hexdigest())
                row.update(source_pages=list(range(1, len(pages) + 1)),
                           explicit_terms=explicit_cash_terms(pages, action),
                           status="machine_extracted_explicit_cash_terms_candidate")
            except ValueError as error:
                row.update(status="needs_review", reason=str(error))
            except Exception as error:
                row.update(status="source_or_reader_error", reason=f"{type(error).__name__}: {error}")
                state["records"].append(row)
                state["status"] = "stopped_direct_source_error"
                write_json(root / "result.json", state)
                raise
            state["records"].append(row)
            write_json(root / "result.json", state)
            print(f"{seed['symbol']} {seed['ex_date']}: {row['status']}", flush=True)
    state.update(status="direct_notice_batch_completed", unattempted_events=0,
                 limitations=["Machine candidates require separate full-text/page review before amendment",
                              "No query recovery, inferred zeros, stock exclusion or account mutation"])
    write_json(root / "result.json", state)
    return root, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--direct-notices", type=Path, help="Explicit known CNinfo/SZSE PDF URLs; fresh candidate acquisition only")
    parser.add_argument("--reuse-root", type=Path)
    parser.add_argument("--index-root", type=Path, help="Explicit completed public discovery-index receipt; notice/PDF checks unchanged")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--pdf-python", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reparse", action="store_true")
    parser.add_argument("--link-plans", action="store_true", help="Link archived pairs; add --execute to fetch missing companions; candidates only")
    parser.add_argument("--with-plans", action="store_true", help="Acquire a unique companion when a final notice lacks explicit share terms; candidates only")
    parser.add_argument("--max-events", type=int, default=20)
    parser.add_argument("--priority-event", action="append", default=[], metavar="SYMBOL:YYYY-MM-DD",
                        help="Collect actual blocked ledger events first; keep the full target universe")
    args = parser.parse_args(argv)
    if args.direct_notices:
        if (not args.event_view or not args.pdf_python or not args.execute or args.root or args.reuse_root
                or args.index_root or args.reparse or args.link_plans or args.with_plans or args.priority_event):
            parser.error("Direct notices require only --event-view --pdf-python --execute --direct-notices")
        folder, result = collect_direct(args.event_view, args.direct_notices, args.pdf_python)
        print(json.dumps({"folder": str(folder), **result}, ensure_ascii=True, indent=2))
        return 0
    if args.index_root and (not args.event_view or args.root or args.link_plans or args.reparse):
        parser.error("Index reuse belongs to a newly prepared event-view batch")
    if args.with_plans and (not args.execute or args.link_plans or args.reparse):
        parser.error("--with-plans requires --execute acquisition; not offline linking/reparsing")
    if args.link_plans:
        if args.root is None or args.pdf_python is None or args.event_view or args.reuse_root or args.reparse:
            parser.error("Use --root --pdf-python with --link-plans, optionally bounded priority events")
        keys = [tuple(value.rsplit(":", 1)) for value in args.priority_event]
        if any(len(key) != 2 for key in keys):
            parser.error("--priority-event requires SYMBOL:YYYY-MM-DD")
        folder, result = link_plans(args.root, args.pdf_python, args.max_events, keys, acquire=args.execute)
        print(json.dumps({"folder": str(folder), **result}, ensure_ascii=True, indent=2))
        return 0
    if args.reparse:
        if args.root is None or args.event_view or args.execute or args.reuse_root or args.priority_event:
            parser.error("Use only --root with --reparse; no network collection")
        root, result = reparse(args.root)
        print(json.dumps({"folder": str(root), **result}, ensure_ascii=True, indent=2))
        return 0
    priority_events = []
    for value in args.priority_event:
        parts = value.rsplit(":", 1)
        if len(parts) != 2:
            parser.error("--priority-event requires SYMBOL:YYYY-MM-DD")
        date.fromisoformat(parts[1])
        priority_events.append(tuple(parts))
    root = args.root
    if args.event_view:
        root = prepare(args.event_view, root, args.reuse_root, priority_events=priority_events, index_root=args.index_root)
    elif args.reuse_root:
        parser.error("--reuse-root requires --event-view to pin the new event view")
    elif root is None:
        parser.error("Use --event-view to prepare or --root to resume")
    result = collect(root, args.pdf_python, args.max_events, priority_events, with_plans=args.with_plans) if args.execute else json.loads((root / "plan.json").read_text(encoding="utf-8"))
    print(json.dumps({"folder": str(root), **result}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
