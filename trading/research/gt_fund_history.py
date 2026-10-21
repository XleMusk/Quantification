"""Public issuer cash-dividend history for the reviewed511010 fund, not an account API."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
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


CODE = "511010"
PAGE = "https://e.gtfund.com/Etrade/Jijin/view/id/511010"
COMMON = "https://e.gtfund.com/Public/static/etrade/1/dist/js/common.js?v=hy186"
VIEW = "https://e.gtfund.com/Public/static/etrade/1/dist/js/jijin_view.js?v=hy186"
API = "https://e.gtfund.com/Etrade/Public/cochin/info.fund.dividend"
PARAMS = {"api": "info.fund.dividend", "params": {"fundCode": CODE}}
KIND = "gt_complete_cash_dividend_history_v1"
CHINA = timezone(timedelta(hours=8))


def local_day(value):
    if not isinstance(value, str):
        raise ValueError("Issuer history needs explicit zoned timestamps")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.astimezone(CHINA).time() != time():
        raise ValueError("Issuer history timestamp is not a zoned China-date midnight")
    return instant.astimezone(CHINA).date().isoformat()


def normalize(payload):
    if not isinstance(payload, list) or not payload:
        raise ValueError("511010 complete history must contain its known dividends, not an empty success")
    rows = []
    for item in payload:
        if (not isinstance(item, dict) or not all(key in item for key in (
                "date", "registerDate", "payDate", "dividend", "date2", "registerDate2", "payDate2"))
                or any(item[key] is not None for key in ("date2", "registerDate2", "payDate2"))):
            raise ValueError("Unsupported dividend-history structure or second share-class dates")
        ex, record, pay = (local_day(item[key]) for key in ("date", "registerDate", "payDate"))
        amount = Decimal(str(item["dividend"]))
        if not record < ex <= pay or not amount.is_finite() or amount <= 0:
            raise ValueError("Invalid issuer dividend chronology or cash per unit")
        rows.append({"instrument_code": CODE, "record_date": record, "ex_date": ex,
                     "pay_date": pay, "cash_rmb_per_unit": str(amount)})
    if len({row["ex_date"] for row in rows}) != len(rows):
        raise ValueError("Duplicate issuer cash dividends")
    return sorted(rows, key=lambda row: row["ex_date"])


def validate_sources(page, common, view, payload):
    soup = BeautifulSoup(page, "html.parser")
    scripts = [script.get_text() for script in soup.find_all("script")]
    options = [text for text in scripts if "var gtOptions" in text]
    if len(options) != 1 or not soup.find(id="jijin_fenhong"):
        raise ValueError("Public issuer page lacks its named dividend-history view")
    match = re.search(r"\bdetail\s*:\s*", options[0])
    if not match:
        raise ValueError("Public issuer page lacks structured fund identity")
    detail, _ = json.JSONDecoder().raw_decode(options[0][match.end():])
    if detail.get("code") != CODE or detail.get("displayCode") != CODE:
        raise ValueError("Public issuer page identifies a different fund")
    as_of = local_day(detail["daily"]["date"])
    if ("/Etrade/Public/cochin/" not in common or "info.fund.dividend" not in view
            or "fundCode: gtOptions.fundcode" not in view):
        raise ValueError("Archived official scripts do not define the reviewed public-history query")
    rows = normalize(payload)
    if any(row["ex_date"] > as_of for row in rows):
        raise ValueError("Public history is newer than its independently dated fund page")
    return {"code": CODE, "name": detail.get("fullName"), "as_of": as_of, "records": rows}


def collect(out=None, *, session=None):
    out = Path(out) if out else DATA_ROOT / "research/fund_issuer_history" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    receipts, contents = {}, {}
    session = session or requests.Session()
    state = {"status": "acquiring", "kind": KIND, "code": CODE, "receipts": receipts,
             "query": PARAMS, "execution_admitted": False, "share_actions_complete": False}
    write_json(out / "result.json", state)
    try:
        for name, url in (("page.html", PAGE), ("common.js", COMMON), ("view.js", VIEW),
                          ("511010_dividends.json", API)):
            response = (session.post(url, json=PARAMS, headers={"Referer": PAGE}, timeout=(5, 18))
                        if url == API else session.get(url, timeout=(5, 18)))
            response.raise_for_status()
            if response.url != url or len(response.content) > 2_000_000:
                raise ValueError("Unexpected public issuer response or size")
            contents[name] = response.content
            (out / name).write_bytes(response.content)
            receipts[name] = {"file": name, "source_url": url, "sha256": sha256(response.content).hexdigest()}
        parsed = validate_sources(contents["page.html"], contents["common.js"].decode("utf-8"),
                                  contents["view.js"].decode("utf-8"), json.loads(contents["511010_dividends.json"]))
        state.update(parsed, status="completed_public_issuer_cash_history",
                     retrieved_at=datetime.now().astimezone().isoformat(),
                     limitations=["Cash-dividend history only; not complete share-action or account evidence",
                                  "Known issuer PDFs must independently match before a period is covered",
                                  "511010 only; no inference for funds managed by other issuers"])
    except Exception as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", state)
        raise
    write_json(out / "result.json", state)
    return out, state


def load(folder):
    folder = Path(folder)
    state = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if (state.get("status") != "completed_public_issuer_cash_history" or state.get("kind") != KIND
            or state.get("query") != PARAMS or state.get("code") != CODE):
        raise ValueError("Expected the completed registered issuer cash-history query")
    contents = {}
    for name, url in (("page.html", PAGE), ("common.js", COMMON), ("view.js", VIEW),
                      ("511010_dividends.json", API)):
        receipt = state["receipts"][name]
        if receipt["file"] != name or receipt["source_url"] != url:
            raise ValueError("Issuer history source identity changed")
        contents[name] = archived(folder, receipt)
    parsed = validate_sources(contents["page.html"], contents["common.js"].decode("utf-8"),
                              contents["view.js"].decode("utf-8"), json.loads(contents["511010_dividends.json"]))
    if any(state[key] != value for key, value in parsed.items()):
        raise ValueError("Issuer history records differ from the archived primary response")
    return state


def period_evidence(folder, events, first, last, code=CODE):
    state = load(folder)
    if (code != CODE or date.fromisoformat(first).isoformat() != first
            or date.fromisoformat(last).isoformat() != last or not "2023-01-03" <= first <= last <= state["as_of"]
            or "instrument_code" not in events or not events.instrument_code.eq(CODE).all()):
        raise ValueError("Issuer cash history does not cover the requested code or dates")
    # Check all independently reviewed modern PDF events, not just the new quarter.
    primary = sorted((row["record_date"], row["ex_date"], row["pay_date"], Decimal(row["cash_rmb_per_unit"]))
                     for row in state["records"] if "2023-01-03" <= row["ex_date"] <= state["as_of"])
    known = sorted((row["record_date"], row["ex_date"], row["pay_date"], Decimal(row["cash_rmb_per_unit"]))
                   for row in events.to_dict("records") if "2023-01-03" <= row["ex_date"] <= state["as_of"])
    if primary != known or not known:
        raise ValueError("Complete issuer cash history conflicts with independently reviewed PDF cash events")
    receipt = state["receipts"]["511010_dividends.json"]
    return {"instrument_code": CODE, "start": first, "end": last, "source_kind": KIND,
            "source_file": receipt["file"], "source_sha256": receipt["sha256"], "source_url": API,
            "as_of": state["as_of"], "independent_pdf_events_matched": len(known),
            "events": [{"ex_date": row["ex_date"], "cash_rmb_per_unit": row["cash_rmb_per_unit"]}
                       for row in state["records"] if first <= row["ex_date"] <= last],
            "share_actions_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = collect(args.out)
    print(json.dumps({"folder": str(folder), "status": result["status"], "as_of": result["as_of"],
                      "cash_events": len(result["records"]), "execution_admitted": False}), flush=True)
    return 0
