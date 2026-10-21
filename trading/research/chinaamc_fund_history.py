"""Observed public ChinaAMC cash histories; missing ex-dates stay PDF-derived."""
import argparse
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

from bs4 import BeautifulSoup
import requests

from .etf_reconciliation import archived
from ..market_dataset import write_json
from ..settings import DATA_ROOT


KIND = "chinaamc_complete_cash_dividend_history_v1"
NAMES = {"510050": "\u534e\u590f\u4e0a\u8bc150ETF",
         "588000": "\u534e\u590f\u4e0a\u8bc1\u79d1\u521b\u677f50\u6210\u4efdETF"}
HOST = "https://fund.chinaamc.com"


def urls(code):
    if code not in NAMES:
        raise ValueError("Only the observed registered ChinaAMC fund pages are supported")
    return {f"{code}_page.html": f"{HOST}/fund/{code}/lishifenhong.shtml?source=click",
            f"{code}_dividends.html": f"{HOST}/product/fundLishifenhong.do?fundcode={code}"}


def parse(page, history, code):
    urls(code)
    page = BeautifulSoup(page, "html.parser")
    identity = f"\uff08\u57fa\u91d1\u4ee3\u7801\uff1a{code}\uff09"
    if (page.title is None or not page.title.get_text().startswith(NAMES[code])
            or identity not in page.get_text(" ", strip=True)):
        raise ValueError("ChinaAMC product identity differs from the requested fund")
    stamps = [re.fullmatch(r"\u51c0\u503c\s*[\uff08(](\d{4}-\d{2}-\d{2})[\uff09)]",
                          node.get_text(" ", strip=True)) for node in page.select(".box2 .item .t2")]
    stamps = [match.group(1) for match in stamps if match]
    frames = page.select("iframe#iframepage")
    if (len(stamps) != 1 or len(frames) != 1
            or frames[0].get("src") != f"/product/fundLishifenhong.do?fundcode={code}"):
        raise ValueError("Missing independently dated NAV or named cash-history frame")
    as_of = date.fromisoformat(stamps[0]).isoformat()
    history = BeautifulSoup(history, "html.parser")
    form = history.select_one("form#queryForm")
    if (form is None or form.get("action") != "fundLishifenhong.do"
            or form.get("method", "").lower() != "post"
            or [(node.get("name"), node.get("value")) for node in form.select("input")]
            != [("fundcode", code)]):
        raise ValueError("History fund identity is missing or the query is filtered")
    scripts = "\n".join(node.get_text() for node in history.find_all("script"))
    for field in ("totalPage", "currentPage"):
        values = re.findall(r"\b(?:let|var|const)\s+" + field + r"\s*=\s*['\"](\d+)['\"]", scripts)
        if not values or set(values) != {"1"}:
            raise ValueError("Cash history is not the complete observed single page")
    headers = [node.get_text(strip=True) for node in history.select("#hoverBox1 th")]
    if headers != ["\u5e74\u4efd", "\u6743\u76ca\u767b\u8bb0\u65e5",
                   "\u6bcf\u4efd\u5206\u7ea2\uff08\u5143\uff09", "\u5206\u7ea2\u53d1\u653e\u65e5"]:
        raise ValueError("Cash-history fields or per-unit RMB semantics changed")
    rows, totals = [], []
    for row in history.select("#hoverBox1_1 tr"):
        cells = [node.get_text(strip=True) for node in row.find_all("td")]
        if len(cells) != 4:
            raise ValueError("Incomplete cash-history row")
        if cells[2].startswith("\u5408\u8ba1:") and not any(cells[i] for i in (0, 1, 3)):
            value = cells[2].split(":", 1)[1]
            totals.append(Decimal(value) if value else None)
            continue
        year, record, cash, pay = cells
        record, pay = date.fromisoformat(record).isoformat(), date.fromisoformat(pay).isoformat()
        amount = Decimal(cash)
        if (year != record[:4] or not record <= as_of or not record < pay
                or not amount.is_finite() or amount <= 0):
            raise ValueError("Invalid record/payment dates or per-unit cash")
        rows.append({"instrument_code": code, "record_date": record,
                     "pay_date": pay, "cash_rmb_per_unit": str(amount)})
    if len(totals) != 1 or len({row["record_date"] for row in rows}) != len(rows):
        raise ValueError("Missing/duplicate total or cash records")
    total = totals[0]
    if total is not None and (not total.is_finite() or total < 0
                             or sum((Decimal(row["cash_rmb_per_unit"]) for row in rows), Decimal(0)) != total):
        raise ValueError("Cash-history records do not reconcile to the reported full total")
    if code == "510050" and (not rows or total is None):
        raise ValueError("510050 history omitted its known distributions")
    complete = bool(rows) and total is not None
    return {"code": code, "name": NAMES[code], "as_of": as_of,
            "records": sorted(rows, key=lambda row: row["record_date"]),
            "reported_total_cash_per_unit": str(total) if total is not None else None,
            "complete_cash_history": complete, "share_actions_complete": False,
            "ex_dates_provided": False,
            "coverage_limitation": None if complete else "Empty history/blank total does not prove zero distributions"}


def collect(codes=("510050", "588000"), out=None, *, session=None):
    codes = list(codes)
    if not codes or len(set(codes)) != len(codes) or not set(codes) <= NAMES.keys():
        raise ValueError("Use distinct observed ChinaAMC fund codes")
    out = Path(out) if out else DATA_ROOT / "research/fund_issuer_history" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    state = {"status": "acquiring", "kind": KIND, "codes": codes, "receipts": {}, "funds": {},
             "execution_admitted": False, "share_actions_complete": False}
    write_json(out / "result.json", state)
    owned = session is None
    session = session or requests.Session()
    try:
        for code in codes:
            content = {}
            for name, url in urls(code).items():
                response = session.get(url, timeout=(5, 18))
                response.raise_for_status()
                if response.url != url or len(response.content) > 2_000_000:
                    raise ValueError("Unexpected issuer response route or size")
                content[name] = response.content
                (out / name).write_bytes(response.content)
                state["receipts"][name] = {"file": name, "source_url": url,
                                            "sha256": sha256(response.content).hexdigest()}
            state["funds"][code] = parse(*content.values(), code)
            write_json(out / "result.json", state)
        state.update(status="completed_public_issuer_cash_history",
                     retrieved_at=datetime.now().astimezone().isoformat())
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", state)
        raise
    finally:
        if owned:
            session.close()
    write_json(out / "result.json", state)
    return out, state


def load(folder):
    folder = Path(folder)
    state = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    codes = state.get("codes", [])
    if (state.get("status") != "completed_public_issuer_cash_history" or state.get("kind") != KIND
            or not codes or len(set(codes)) != len(codes) or not set(codes) <= NAMES.keys()
            or set(state.get("funds", {})) != set(codes)):
        raise ValueError("Expected the completed registered ChinaAMC cash-history acquisition")
    expected = {name: url for code in codes for name, url in urls(code).items()}
    if set(state["receipts"]) != set(expected):
        raise ValueError("Unexpected history archive files")
    for code in codes:
        content = []
        for name, url in urls(code).items():
            receipt = state["receipts"][name]
            if receipt["file"] != name or receipt["source_url"] != url:
                raise ValueError("ChinaAMC source identity changed")
            content.append(archived(folder, receipt))
        if parse(*content, code) != state["funds"][code]:
            raise ValueError("Cash-history records differ from the archived primary page")
    return state


def period_evidence(folder, events, first, last, code):
    state = load(folder)
    fund = state["funds"].get(code)
    if (fund is None or not fund["complete_cash_history"]
            or date.fromisoformat(first).isoformat() != first or date.fromisoformat(last).isoformat() != last
            or not "2023-01-03" <= first <= last <= fund["as_of"]
            or "instrument_code" not in events or not events.instrument_code.eq(code).all()):
        raise ValueError("ChinaAMC history does not establish coverage for the requested code or dates")
    # This official table lacks ex-dates; only independent PDFs supply them.
    primary = sorted((row["record_date"], row["pay_date"], Decimal(row["cash_rmb_per_unit"]))
                     for row in fund["records"] if "2023-01-03" <= row["record_date"] <= fund["as_of"])
    known = sorted((row["record_date"], row["pay_date"], Decimal(row["cash_rmb_per_unit"]))
                   for row in events.to_dict("records") if "2023-01-03" <= row["record_date"] <= fund["as_of"])
    if not known or primary != known:
        raise ValueError("Complete history conflicts with independently reviewed PDF cash events")
    receipt = state["receipts"][f"{code}_dividends.html"]
    return {"instrument_code": code, "start": first, "end": last, "source_kind": KIND,
            "source_file": receipt["file"], "source_sha256": receipt["sha256"], "source_url": receipt["source_url"],
            "as_of": fund["as_of"], "independent_pdf_events_matched": len(known),
            "ex_dates_from": "independently_reviewed_issuer_pdfs",
            "events": sorted([{"ex_date": row["ex_date"], "cash_rmb_per_unit": row["cash_rmb_per_unit"]}
                              for row in events.to_dict("records") if first <= row["ex_date"] <= last],
                             key=lambda row: row["ex_date"]), "share_actions_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", default="510050,588000")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, state = collect(args.codes.split(","), args.out)
    print(json.dumps({"folder": str(folder), "funds": {code: {
        "as_of": fund["as_of"], "records": len(fund["records"]),
        "complete_cash_history": fund["complete_cash_history"]}
        for code, fund in state["funds"].items()}, "execution_admitted": False}), flush=True)
    return 0
