"""Batch primary issuer-PDF risk extraction with explicit partial coverage."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import uuid

import requests

from ..notice_risk import COVERAGE, PARSER_ID, TIME_BASIS, checked_notice_url, known_at_proxy
from ..settings import DATA_ROOT
from .action_disclosures import PDF_READER, fetch
from .experiment import write_json


POSITIVE = re.compile(r"(?:本公司|公司|发行人)(?:股票)?(?:存在|仍存在|可能存在|面临|可能面临)重大违法强制退市(?:的)?风险")
CONTRADICTION = re.compile(r"(?:本公司|公司|发行人)(?:股票)?(?:不存在|不涉及|未触及)重大违法强制退市|重大违法强制退市(?:的)?风险(?:已消除|已解除)")
CORROBORATION = re.compile(r"财务(?:信息|报告)严重不实|涉嫌重大财务造假|(?:收到|出具|作出)[^。；]{0,40}行政处罚(?:事先告知书|决定书)")


def extract_event(pages, notice):
    symbol, published, url = notice["symbol"], notice["publication_on"], notice["source_url"]
    checked_notice_url(symbol, published, url)
    if not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
        raise ValueError("Every PDF page needs text; scanned/OCR notices require review")
    compact_pages = [re.sub(r"\s+", "", page) for page in pages]
    text = "".join(compact_pages)
    code = symbol.split(".")[0]
    if not re.search(r"(?:证券|股票)代码[:：]?" + code + r"(?![0-9])", compact_pages[0]):
        raise ValueError("Notice issuer header differs from requested stock")
    footer = re.findall(r"董事会([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", compact_pages[-1][-150:])
    if len(footer) != 1 or date(*map(int, footer[0])).isoformat() != published:
        raise ValueError("Dated archive route and issuer footer must agree")
    candidates = []
    for index, page in enumerate(compact_pages, 1):
        for match in POSITIVE.finditer(page):
            if re.search(r"(?:如|若|如果|一旦|假设)$", page[max(0, match.start() - 5):match.start()]):
                continue
            candidates.append(index)
    if not candidates:
        return None
    if CONTRADICTION.search(text) or not CORROBORATION.search(text):
        raise ValueError("Risk affirmation is contradictory or lacks the supported factual clause; needs review")
    return {"id": symbol + ":" + published + ":" + sha256(url.encode()).hexdigest()[:12],
            "symbol": symbol, "publication_on": published, "known_at_proxy": known_at_proxy(published),
            "category": "major_illegal_delisting_risk", "parser": PARSER_ID,
            "source_url": url, "evidence_pages": sorted(set(candidates)),
            "classification": "explicit_issuer_risk_warning_not_confirmed_delisting_or_fraud_verdict"}


def collect(plan_file, pdf_python):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    notices = plan["notices"]
    if not isinstance(notices, list) or not 1 <= len(notices) <= 64 or not Path(pdf_python).is_file():
        raise ValueError("Existing PDF interpreter and bounded1..64 primary notice plan required")
    keys = set()
    for notice in notices:
        checked_notice_url(notice["symbol"], notice["publication_on"], notice["source_url"])
        if notice["source_url"] in keys:
            raise ValueError("Duplicate planned primary notice")
        keys.add(notice["source_url"])
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/primary_risk_notices" / identifier
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "plan.json", plan)
    records, events = [], []
    with requests.Session() as session:
        for index, notice in enumerate(notices):
            record = {**notice, "retrieved_at": datetime.now().astimezone().isoformat()}
            folder = root / "sources" / str(index + 1)
            folder.mkdir(parents=True, exist_ok=False)
            try:
                raw = fetch(session, "GET", notice["source_url"])
                if not raw.startswith(b"%PDF-"):
                    raise ValueError("Primary notice response is not a PDF")
                pdf = folder / "notice.pdf"
                pdf.write_bytes(raw)
                record.update(source_file=pdf.relative_to(root).as_posix(), source_sha256=sha256(raw).hexdigest())
                result = subprocess.run([str(pdf_python), "-X", "utf8", "-c", PDF_READER, str(pdf)],
                                        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
                pages = json.loads(result.stdout)["pages"]
                write_json(folder / "text.json", {"pages": pages})
                record.update(extracted_pages=len(pages), text_file=(folder / "text.json").relative_to(root).as_posix())
                event = extract_event(pages, notice)
                record["status"] = "explicit_positive_event" if event else "no_supported_positive_match_not_safe"
                if event:
                    event.update(source_file=record["source_file"], source_sha256=record["source_sha256"],
                                 retrieved_at=record["retrieved_at"])
                    events.append(event)
            except (ValueError, requests.RequestException, subprocess.SubprocessError, OSError) as error:
                record.update(status="needs_review_or_source_error", error=f"{type(error).__name__}: {error}")
            records.append(record)
            write_json(root / "records.json", records)
            print(f"{index+1}/{len(notices)} {notice['symbol']} {record['status']}", flush=True)
    write_json(root / "events.json", events)
    manifest = {"status": "primary_notice_risk_diagnostic_v1", "parser": PARSER_ID,
        "events": len(events), "events_sha256": sha256((root / "events.json").read_bytes()).hexdigest(),
        "planned_notices": len(notices), "source_or_review_errors": sum("error" in row for row in records),
        "coverage": COVERAGE, "time_basis": TIME_BASIS, "replay_only": True, "execution_admitted": False,
        "basis": plan.get("basis", "Explicit input notice plan; not a complete/PIT market notice corpus"),
        "created_at": datetime.now().astimezone().isoformat()}
    write_json(root / "manifest.json", manifest)
    print(json.dumps({"output": str(root), **manifest}, ensure_ascii=False), flush=True)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--pdf-python", type=Path, required=True)
    args = parser.parse_args(argv)
    root = collect(args.plan, args.pdf_python)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return 1 if manifest["source_or_review_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
