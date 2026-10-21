"""Bounded public SSE fund disclosures; metadata is not an action/trading feed."""

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urljoin, urlparse

import requests

from ..market_dataset import write_json
from ..settings import DATA_ROOT


PAGE = "https://www.sse.com.cn/disclosure/fund/announcement/"
RECIPE = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_fundInformation_2021.js"
ENDPOINT = "https://query.sse.com.cn/commonQuery.do"
SQL_ID = "COMMON_PL_JJXX_JJGG_NEW_L"
PAGE_SIZE = 100
MAX_PAGES = 10


def parameters(code, start, end, page):
    if not isinstance(code, str) or not re.fullmatch(r"5[0-9]{5}", code):
        raise ValueError("Expected a six-digit SSE fund code, not an inferred fund type")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last - first).days > 1096:
        raise ValueError("SSE disclosures require an ordered range of at most three years")
    if type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError("Disclosure pagination exceeds the bounded collection")
    return {"isPagination": "true", "pageHelp.pageSize": PAGE_SIZE,
            "pageHelp.pageNo": page, "pageHelp.beginPage": page,
            "pageHelp.cacheSize": 1, "pageHelp.endPage": page, "type": "inParams",
            "sqlId": SQL_ID, "SECURITY_CODE": code, "START_DATE": start,
            "END_DATE": end, "DATE_DESC": 1}


def document_url(value):
    if not isinstance(value, str):
        raise ValueError("Missing disclosure URL")
    parsed = urlparse(urljoin(PAGE, value))
    if (parsed.scheme != "https" or parsed.hostname not in {"www.sse.com.cn", "static.sse.com.cn"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not parsed.path.startswith("/disclosure/fund/announcement/c/new/")
            or not parsed.path.lower().endswith(".pdf") or parsed.query or parsed.fragment):
        raise ValueError("Disclosure document is outside the reviewed public SSE PDF route")
    return parsed.geturl()


def validate_page(payload, code, start, end, page):
    if (not isinstance(payload, dict) or payload.get("sqlId") != SQL_ID
            or payload.get("actionErrors") or payload.get("fieldErrors")):
        raise ValueError("SSE disclosure query returned an error or different recipe")
    helper, rows = payload.get("pageHelp"), payload.get("result")
    if not isinstance(helper, dict) or not isinstance(rows, list):
        raise ValueError("Missing disclosure page/rows")
    total, pages = helper.get("total"), helper.get("pageCount")
    if (type(total) is not int or total < 0 or type(pages) is not int
            or helper.get("pageNo") != page or helper.get("pageSize") != PAGE_SIZE
            or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
            or pages > MAX_PAGES or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))):
        raise ValueError("SSE disclosure pagination is incomplete or exceeds the bound")
    result = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("SECURITY_CODE") != code
                or not isinstance(row.get("TITLE"), str) or not row["TITLE"].strip()):
            raise ValueError("Disclosure security/title mismatch")
        day = date.fromisoformat(row["SSEDATE"]).isoformat()
        if not start <= day <= end:
            raise ValueError("Disclosure publication outside requested range")
        result.append({**row, "source_url": document_url(row.get("URL"))})
    if len({row["source_url"] for row in result}) != len(result):
        raise ValueError("Duplicate disclosure within a page")
    return result, total, pages


def fetch(session, params):
    with session.get(ENDPOINT, params=params, headers={"Referer": PAGE},
                     timeout=(5, 22), stream=True) as response:
        response.raise_for_status()
        if urlparse(response.url).hostname != "query.sse.com.cn":
            raise ValueError("SSE query redirected away from its public data host")
        parts, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 2_000_000:
                raise ValueError("SSE disclosure page exceeds 2 MB")
            parts.append(chunk)
    return b"".join(parts)


def collect(codes, start, end, folder=None, session=None):
    codes = list(codes)
    if not 1 <= len(codes) <= 64 or len(set(codes)) != len(codes):
        raise ValueError("Use one bounded batch of 1-64 distinct SSE fund codes")
    for code in codes:
        parameters(code, start, end, 1)
    folder = Path(folder) if folder else DATA_ROOT / "research/fund_disclosures" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    state = {"status": "collecting", "source": "SSE public fund disclosures",
             "recipe_url": RECIPE, "endpoint": ENDPOINT, "start": start, "end": end,
             "requested_codes": codes, "completed_codes": [], "receipts": [], "records": [],
             "created_at": datetime.now().astimezone().isoformat(),
             "execution_admitted": False, "strategy_validated": False,
             "limits": ["SSE only, not Shenzhen funds or all ETFs",
                        "Public announcement index is not proof of zero cash/share actions",
                        "No document interpretation, prices, statuses, adjustment or account changes",
                        "Retrieval-time index completeness is not historical signal availability"]}
    write_json(folder / "result.json", state)
    owned_session = session is None
    session = requests.Session() if owned_session else session
    try:
        for code in codes:
            all_rows, expected_total, expected_pages = [], None, None
            for page in range(1, MAX_PAGES + 1):
                params = parameters(code, start, end, page)
                raw = fetch(session, params)
                relative = f"{code}_page_{page}.json"
                (folder / relative).write_bytes(raw)
                state["receipts"].append({"file": relative, "params": params,
                                          "sha256": sha256(raw).hexdigest(),
                                          "retrieved_at": datetime.now().astimezone().isoformat()})
                write_json(folder / "result.json", state)
                rows, total, pages = validate_page(json.loads(raw), code, start, end, page)
                if expected_total is not None and (total, pages) != (expected_total, expected_pages):
                    raise ValueError("Disclosure totals changed while paging; do not merge different index states")
                expected_total, expected_pages = total, pages
                all_rows.extend(rows)
                if page >= pages:
                    break
            if (len(all_rows) != expected_total
                    or len({row["source_url"] for row in all_rows}) != len(all_rows)):
                raise ValueError("Missing or repeated disclosures across pages")
            state["records"].extend(all_rows)
            state["completed_codes"].append(code)
            write_json(folder / "result.json", state)
            print(f"{code}: {len(all_rows)} public disclosure records", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="stopped_source_error", failed_code=code,
                     error=f"{type(error).__name__}: {error}")
        write_json(folder / "result.json", state)
        raise
    finally:
        if owned_session:
            session.close()
    state["status"] = "completed_public_disclosure_index"
    write_json(folder / "result.json", state)
    return folder, state


def load_index(folder):
    folder = Path(folder)
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if (result.get("status") != "completed_public_disclosure_index"
            or result.get("completed_codes") != result.get("requested_codes")):
        raise ValueError("Expected a completed public disclosure index")
    records = []
    for receipt in result["receipts"]:
        params = receipt["params"]
        code, page = params["SECURITY_CODE"], params["pageHelp.pageNo"]
        if (code not in result["requested_codes"]
                or params != parameters(code, result["start"], result["end"], page)
                or receipt["file"] != f"{code}_page_{page}.json"):
            raise ValueError("Saved disclosure query identity changed")
        raw = (folder / receipt["file"]).read_bytes()
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("Saved disclosure page hash changed")
        rows, _, _ = validate_page(json.loads(raw), code, result["start"], result["end"], page)
        records.extend(rows)
    if records != result["records"]:
        raise ValueError("Saved disclosure index differs from its archived pages")
    return result


def fetch_pdf(session, url):
    document_url(url)
    with session.get(url, headers={"Referer": PAGE}, timeout=(5, 30), stream=True) as response:
        response.raise_for_status()
        document_url(response.url)
        parts, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 20_000_000:
                raise ValueError("Fund report exceeds 20 MB")
            parts.append(chunk)
    return b"".join(parts)


def download_documents(index, kind="interim", folder=None, session=None, codes=None, report_year=None):
    result = load_index(index)
    if report_year is not None and kind != "annual":
        raise ValueError("A report year is only valid for annual documents")
    records = result["records"]
    if codes is not None:
        codes = list(codes)
        if not codes or len(set(codes)) != len(codes) or not set(codes) <= set(result["completed_codes"]):
            raise ValueError("Document codes must be distinct members of the saved disclosure index")
        records = [row for row in records if row["SECURITY_CODE"] in codes]
    if kind == "interim":
        suffix = result["end"][:4] + "\u5e74\u4e2d\u671f\u62a5\u544a"
        documents = [row for row in records if row["TITLE"].endswith(suffix)]
        if (not 1 <= len(documents) <= 8
                or len({row["SECURITY_CODE"] for row in documents}) != len(documents)):
            raise ValueError("Select an index containing 1-8 distinct current-year interim reports")
    elif kind == "annual":
        if type(report_year) is not int or not 2023 <= report_year < int(result["end"][:4]):
            raise ValueError("Specify a supported annual report year preceding the index end year")
        suffix = str(report_year) + "\u5e74\u5e74\u5ea6\u62a5\u544a"
        documents = [row for row in records if row["TITLE"].endswith(suffix)]
        if (not 1 <= len(documents) <= 8
                or len({row["SECURITY_CODE"] for row in documents}) != len(documents)
                or (codes is not None and {row["SECURITY_CODE"] for row in documents} != set(codes))):
            raise ValueError("Select exactly one complete annual report per requested code; not summaries")
    elif kind == "distribution":
        documents = [row for row in records
                     if row.get("ORG_BULLETIN_TYPE_DESC") == "\u6536\u76ca\u5206\u914d"
                     and row["TITLE"].endswith(("\u5206\u7ea2\u516c\u544a", "\u6536\u76ca\u5206\u914d\u516c\u544a"))]
        if not 1 <= len(documents) <= 16:
            raise ValueError("Select an index containing 1-16 explicit cash-distribution announcements")
    else:
        raise ValueError("Unsupported reviewed fund document kind")
    names = [f"{row['SECURITY_CODE']}_{row['SSEDATE']}_{kind}.pdf" for row in documents]
    if len(set(names)) != len(names):
        raise ValueError("Multiple disclosures share an archive name; preserve them without overwriting")
    folder = Path(folder) if folder else DATA_ROOT / "research/fund_documents" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    state = {"status": "collecting", "document_kind": kind, "index": str(Path(index).resolve()),
             "index_sha256": sha256((Path(index) / "result.json").read_bytes()).hexdigest(),
             "requested_documents": documents, "receipts": [], "execution_admitted": False,
             "limits": ["Archived issuer reports, not reviewed cash/share actions or a fund execution feed"]}
    if kind == "annual":
        state["report_year"] = report_year
    write_json(folder / "result.json", state)
    owned_session = session is None
    session = requests.Session() if owned_session else session
    try:
        for row in documents:
            code = row["SECURITY_CODE"]
            raw = fetch_pdf(session, row["source_url"])
            relative = f"{code}_{row['SSEDATE']}_{kind}.pdf"
            (folder / relative).write_bytes(raw)
            if not raw.startswith(b"%PDF-"):
                raise ValueError("Fund report response is not a PDF; raw response preserved")
            state["receipts"].append({"code": code, "source_url": row["source_url"],
                                      "file": relative, "sha256": sha256(raw).hexdigest(),
                                      "bytes": len(raw), "retrieved_at": datetime.now().astimezone().isoformat()})
            write_json(folder / "result.json", state)
            print(f"{code}: issuer PDF archived", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="stopped_source_error", failed_code=code,
                     error=f"{type(error).__name__}: {error}")
        write_json(folder / "result.json", state)
        raise
    finally:
        if owned_session:
            session.close()
    state["status"] = "completed_primary_document_acquisition"
    write_json(folder / "result.json", state)
    return folder, state


def download_interim(index, folder=None, session=None):
    return download_documents(index, "interim", folder, session)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="Comma-separated SSE fund codes; with --index, select only saved members")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--index", type=Path, help="Archive reviewed document kinds from a completed saved index")
    parser.add_argument("--document-kind", choices=("interim", "annual", "distribution"), default="interim")
    parser.add_argument("--report-year", type=int, help="Exact annual report year, not publication year")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.index:
        if args.start or args.end:
            parser.error("A saved index fixes dates; do not change them during document acquisition")
        folder, result = download_documents(args.index, args.document_kind, args.out,
                                          codes=args.codes.split(",") if args.codes else None,
                                          report_year=args.report_year)
        counts = {"documents": len(result["receipts"])}
    else:
        if args.document_kind != "interim" or args.report_year is not None:
            parser.error("Document selection requires a completed saved index")
        if not all((args.codes, args.start, args.end)):
            parser.error("Specify codes/start/end or a completed index")
        folder, result = collect(args.codes.split(","), args.start, args.end, args.out)
        counts = {"codes": len(result["completed_codes"]), "records": len(result["records"])}
    print(json.dumps({"folder": str(folder), "status": result["status"],
                      **counts,
                      "execution_admitted": False}), flush=True)
    return 0
