"""Link explicit post-event issuer confirmations for research accounting only."""
import argparse
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import pandas as pd
import requests

from ..market_dataset import normalize_symbol, write_json
from ..market_snapshot import RESERVE_CORRECTION_FIELDS
from ..settings import DATA_ROOT
from .action_disclosures import DAY, NoticeTitle, PDF_READER, QUERY, cash_only_statements, explicit_cash_terms, fetch, notice_url
from .adjusted_prices import checked_file

KIND = "issuer_postevent_cash_only_terms_diagnostic"
LIMITATION = ("Explicit issuer post-event cash-only confirmations reconstruct historical accounting; "
              "confirmation was NOT known at the event. Replay-only, not PIT signals or execution admission")
TITLE = re.compile(r"\u5b9e\u65bd(20[0-9]{2})\u5e74(?:\u5e74)?\u5ea6\u6743\u76ca\u5206\u6d3e\u540e"
                   r"\u8c03\u6574\u80a1\u4efd\u56de\u8d2d\u4ef7\u683c\u4e0a\u9650")


def document(pages, symbol, publication):
    if not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
        raise ValueError("Complete text-bearing issuer documents required")
    texts = [re.sub(r"\s+", "", page) for page in pages]
    code = symbol.split(".")[0]
    if not re.search(r"\u8bc1\u5238\u4ee3\u7801[:\uff1a]?" + code + r"(?![0-9])", texts[0]):
        raise ValueError("Confirmation/final issuer conflict")
    footer = {date(*map(int, row)).isoformat() for row in
              re.findall(r"\u8463\u4e8b\u4f1a" + DAY, texts[-1][-180:])}
    if footer != {publication}:
        raise ValueError("Dated archive route and issuer footer differ")
    return "".join(texts)


def extract(final_pages, confirmation_pages, action, final_date, confirmation_date):
    if (normalize_symbol(action["symbol"]).baostock_code != action["code"]
            or action["dividStockMarketDate"] != ""):
        raise ValueError("Unchanged ordinary cash-only issuer identity/listing terms required")
    final = document(final_pages, action["symbol"], final_date)
    confirmation = document(confirmation_pages, action["symbol"], confirmation_date)
    ex = date.fromisoformat(action["dividOperateDate"])
    if not (date.fromisoformat(final_date) <= date.fromisoformat(action["dividRegistDate"]) < ex
            <= date.fromisoformat(action["dividPayDate"]) < date.fromisoformat(confirmation_date) <= ex + timedelta(days=7)):
        raise ValueError("Explicit pre-record implementation and bounded POST-event confirmation required")
    final_years = set(re.findall(r"(20[0-9]{2})\u5e74(?:\u5e74)?\u5ea6\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a", final))
    if len(final_years) != 1 or set(TITLE.findall(confirmation)) != final_years:
        raise ValueError("Confirmation must identify the same unique annual distribution")
    numbers = set(re.findall(r"\u516c\u544a\u7f16\u53f7[:\uff1a]([0-9]{4}-[0-9]+)", final))
    if len(numbers) != 1 or not re.search(r"\u516c\u544a\u7f16\u53f7[:\uff1a]" + re.escape(next(iter(numbers))) + r"(?![0-9])", confirmation):
        raise ValueError("Confirmation must explicitly reference the implementation notice number")
    if len(cash_only_statements(confirmation_pages)) != 1:
        raise ValueError("Explicit current-event no-bonus AND no-transfer factual clause required")
    # Real documents supply the clause; never synthesize missing zero-share text.
    terms = explicit_cash_terms([*final_pages, *confirmation_pages], action)
    return {"kind": KIND, "symbol": action["symbol"], "code": action["code"],
            **{field: action[field] for field in ("dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate")},
            **terms, "expected_provider_terms": {field: action[field] for field in RESERVE_CORRECTION_FIELDS},
            "final_source_date": final_date, "source_date": confirmation_date,
            "confirmation_known_after_event": True, "basis": LIMITATION, "execution_admitted": False,
            "implementation_notice_number": next(iter(numbers))}


def collect(disclosures_root, symbol, ex, pdf_python, *, reuse_source_root=None):
    disclosures_root, pdf_python = Path(disclosures_root).resolve(), Path(pdf_python)
    if normalize_symbol(symbol).vt_symbol != symbol or not pdf_python.is_file():
        raise ValueError("Canonical issuer and existing PDF interpreter required")
    archived = json.loads((disclosures_root / "result.json").read_text(encoding="utf-8"))
    records = [r for r in archived["records"] if (r["symbol"], r["date"]) == (symbol, ex)]
    if archived["status"] != "batch_completed" or len(records) != 1:
        raise ValueError("One archived terminal implementation notice required")
    entry = records[0]
    if not {"source_file", "text_file", "source_url", "source_date", "query_params"} <= entry.keys():
        raise ValueError("Archived primary implementation document missing")
    actions = pd.read_parquet(disclosures_root / "input_actions.parquet").fillna("")
    actions = actions.loc[actions.symbol.eq(symbol) & actions.dividOperateDate.eq(ex)]
    if len(actions) != 1:
        raise ValueError("Unique pinned canonical event required")
    action = actions.iloc[0].to_dict()
    if {f: action[f] for f in RESERVE_CORRECTION_FIELDS} != entry["expected_provider_terms"]:
        raise ValueError("Archived implementation source terms changed")
    start, end = date.fromisoformat(ex), date.fromisoformat(ex) + timedelta(days=7)
    params = {**entry["query_params"], "seDate": f"{start}~{end}",
              "searchkey": "\u80a1\u4efd\u56de\u8d2d\u4ef7\u683c\u4e0a\u9650"}
    folder = DATA_ROOT / "research/stock_action_confirmations" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    result = {"status": "collecting", "execution_admitted": False, "disclosures_root": str(disclosures_root),
              "symbol": symbol, "event": ex, "query_params": params, "basis": LIMITATION, "records": []}
    write_json(folder / "result.json", result)
    try:
        final_pdf = checked_file(disclosures_root, entry["source_file"], entry["source_sha256"])
        final_text = checked_file(disclosures_root, entry["text_file"], entry["text_sha256"])
        reused = None
        if reuse_source_root is not None:
            reuse_source_root = Path(reuse_source_root).resolve()
            reused = json.loads((reuse_source_root / "result.json").read_text(encoding="utf-8"))
            if (reused["status"] not in {"completed_postevent_confirmation", "unresolved_postevent_confirmation"}
                    or reused["query_params"] != params or reused["symbol"] != symbol or reused["event"] != ex):
                raise ValueError("Reuse requires matching terminal confirmation source queries")
            result["reused_source_root"] = str(reuse_source_root)
        with requests.Session() as session:
            raw = (checked_file(reuse_source_root, "metadata.json", reused["files"]["metadata.json"]).read_bytes()
                   if reused is not None else fetch(session, "POST", QUERY, data=params))
            (folder / "metadata.json").write_bytes(raw)
            page = json.loads(raw)
            if page.get("hasMore") or not isinstance(page.get("announcements"), list):
                raise ValueError("Complete bounded confirmation metadata required")
            matches = [r for r in page["announcements"] if r.get("secCode") == symbol.split(".")[0]
                       and TITLE.search(NoticeTitle(r.get("announcementTitle", "")).text.replace(" ", ""))]
            if len(matches) != 1:
                raise ValueError("One matching issuer confirmation required; no choosing among revisions")
            url = notice_url(matches[0]["adjunctUrl"])
            publication = url.split("/")[-2]
            raw_pdf = (checked_file(reuse_source_root, "confirmation.pdf", reused["files"]["confirmation.pdf"]).read_bytes()
                       if reused is not None else fetch(session, "GET", url))
        if not raw_pdf.startswith(b"%PDF-"):
            raise ValueError("Confirmation response is not a PDF")
        (folder / "confirmation.pdf").write_bytes(raw_pdf)
        parsed = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(folder / "confirmation.pdf")],
                                check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        pages = json.loads(parsed.stdout)["pages"]
        write_json(folder / "confirmation_text.json", {"pages": pages})
        row = extract(json.loads(final_text.read_text(encoding="utf-8"))["pages"], pages, action, entry["source_date"], publication)
        shutil.copy2(final_pdf, folder / "implementation.pdf")
        shutil.copy2(final_text, folder / "implementation_text.json")
        row.update(source_file="confirmation.pdf", source_url=url, source_sha256=sha256(raw_pdf).hexdigest(),
                   source_text_file="confirmation_text.json", final_source_file="implementation.pdf",
                   final_source_url=entry["source_url"], final_source_sha256=entry["source_sha256"],
                   final_text_file="implementation_text.json", retrieved_at=datetime.now().astimezone().isoformat())
        result.update(status="completed_postevent_confirmation", records=[row])
    except (ValueError, KeyError, OSError, requests.RequestException, subprocess.SubprocessError) as error:
        result.update(status="unresolved_postevent_confirmation", error=f"{type(error).__name__}: {error}")
    result["files"] = {p.name: sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file() and p.name != "result.json"}
    write_json(folder / "result.json", result)
    return folder, result


def load_confirmations(root, symbols):
    root = Path(root).resolve()
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "completed_postevent_confirmation" or result["execution_admitted"] is not False or result["basis"] != LIMITATION:
        raise ValueError("Completed replay-only confirmation source required")
    rows = []
    for row in result["records"]:
        if row["symbol"] not in symbols:
            continue
        pages = []
        for stem, text_field in (("final_source", "final_text_file"), ("source", "source_text_file")):
            url = row[stem + "_url"]
            if url != notice_url(url.removeprefix("https://static.cninfo.com.cn/")) or url.split("/")[-2] != row[stem + "_date"]:
                raise ValueError("Official dated confirmation/implementation source required")
            raw = checked_file(root, row[stem + "_file"], row[stem + "_sha256"]).read_bytes()
            if not raw.startswith(b"%PDF-"):
                raise ValueError("Stored confirmation/implementation is not a PDF")
            pages.append(json.loads(checked_file(root, row[text_field], result["files"][row[text_field]]).read_text(encoding="utf-8"))["pages"])
        action = {**row["expected_provider_terms"], "symbol": row["symbol"], "dividOperateDate": row["dividOperateDate"]}
        expected = extract(*pages, action, row["final_source_date"], row["source_date"])
        if any(row.get(k) != v for k, v in expected.items()):
            raise ValueError("Post-event confirmation terms changed")
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("disclosures-root", "pdf-python"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--event", required=True, help="SYMBOL:YYYY-MM-DD")
    parser.add_argument("--reuse-source-root", type=Path, help="Reparse matching archived source bytes into a NEW snapshot")
    args = parser.parse_args(argv)
    parts = args.event.rsplit(":", 1)
    if len(parts) != 2:
        parser.error("Expected SYMBOL:YYYY-MM-DD")
    folder, result = collect(args.disclosures_root, *parts, args.pdf_python, reuse_source_root=args.reuse_source_root)
    print(json.dumps({"folder": str(folder), **result}, ensure_ascii=False))
    return 0 if result["status"] == "completed_postevent_confirmation" else 1
