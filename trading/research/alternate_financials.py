"""Bounded public financial-statement cache, preserving revisions and unknown units."""
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import shutil
import time
import uuid

import pandas as pd
import requests

from a_stock_committee.market_data import normalize_symbol
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json


ENDPOINT = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
SOURCE_CODE = "https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py"
STATEMENTS = ("fzb", "lrb", "llb")
DENOMINATOR_FIELDS = {
    "total_assets": ("fzb", "TOTASSET"), "total_liabilities": ("fzb", "TOTLIAB"),
    "total_equity": ("fzb", "RIGHAGGR"), "parent_equity": ("fzb", "PARESHARRIGH"),
    "monetary_funds": ("fzb", "CURFDS"), "revenue": ("lrb", "BIZINCO"),
    "total_net_profit": ("lrb", "NETPROFIT"), "parent_net_profit": ("lrb", "PARENETP"),
}
LIMITS = [
    "Retrieval-time provider statement history, not verified point-in-time financial data",
    "Publish date and update timestamp are preserved separately; later revisions are not backfilled into historical strategies",
    "Provider currency is retained but numeric units and consolidated/parent/YTD semantics require reconciliation",
    "Blank, null, section headings and invalid numbers remain distinct; missing fields are never zero",
    "Original item order, codes, titles and duplicate fields are retained rather than silently dropping duplicates",
    "No financial ratio construction, annualization, missing-denominator invention or strategy/account mutation; NO_TRADE",
    "BaoStock blacklist is not retried or bypassed; this is a different public provider, not restored BaoStock access",
]


def canonical_day(value):
    if value in (None, ""):
        return None
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = text[:4] + "-" + text[4:6] + "-" + text[6:]
    if date.fromisoformat(text).isoformat() != text:
        raise ValueError("Unknown statement date format")
    return text


def decode_body(body):
    # This endpoint may send GB-encoded JSON despite an incompatible HTTP charset.
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = body.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if "\ufffd" in text:
            raise ValueError("Replacement characters in statement payload")
        return json.loads(text), encoding
    raise ValueError("No supported strict statement encoding")


def numeric_value(value, is_section):
    if value is None:
        return None, "null"
    if value == "":
        return None, "section" if is_section else "blank"
    if value in ("--", "-", "N/A"):
        return None, "missing_marker"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        converted = float(number)
        if converted == float("inf") or converted == float("-inf"):
            raise InvalidOperation
        return converted, "observed_numeric"
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None, "invalid_numeric"


def decode_statement(body, symbol, statement, start, end):
    if statement not in STATEMENTS:
        raise ValueError("Unknown statement kind")
    payload, encoding = decode_body(body)
    result = payload["result"]
    if result["status"].get("code") != 0:
        raise ValueError("Sina statement source error: " + str(result["status"]))
    data = result["data"]
    reports, items = [], []
    seen = set()
    for header in data["report_date"]:
        key = str(header["date_value"])
        day = canonical_day(key)
        if day in seen:
            raise ValueError("Duplicate statement report identity")
        seen.add(day)
        if not start <= day <= end:
            continue
        report = data["report_list"][key]
        publish = canonical_day(report.get("publish_date"))
        metadata = {"symbol": symbol, "statement": statement, "report_day": day,
                    "publish_day": publish, "provider_update_time": report.get("update_time"),
                    "currency_raw": report.get("rCurrency"), "report_type_raw": report.get("rType"),
                    "data_source_raw": report.get("data_source"), "audit_raw": report.get("is_audit"),
                    "report_description_raw": header.get("date_description"),
                    "period_kind_raw": header.get("date_type"), "units_verified": False, "pit_verified": False}
        reports.append(metadata)
        for position, item in enumerate(report["data"]):
            value = item.get("item_value")
            number, status = numeric_value(value, item.get("item_display_type") == 1)
            items.append(metadata | {"position": position, "item_field": item.get("item_field"),
                "item_title": item.get("item_title"), "item_source": item.get("item_source"),
                "item_display_type": item.get("item_display_type"), "raw_value_json": json.dumps(value, ensure_ascii=False),
                "value_numeric_unscaled": number, "value_status": status})
    reported = int(data["report_count"])
    range_not_truncated = reported <= len(data["report_date"]) or bool(seen and min(seen) <= start)
    return pd.DataFrame(items), pd.DataFrame(reports), {"encoding": encoding,
        "reported_history_count": reported, "received_history_count": len(data["report_date"]),
        "earliest_received_report": min(seen) if seen else None,
        "requested_range_not_truncated": range_not_truncated,
        "requested_periods": len(reports), "item_rows": len(items), "pit_verified": False, "units_verified": False}


def collect(symbols, start, end, request_get=requests.get, recover_after=None):
    start, end = canonical_day(start), canonical_day(end)
    if not start or not end or not start <= end <= date.today().isoformat():
        raise ValueError("Canonical past/current statement period required")
    symbols = [normalize_symbol(symbol).vt_symbol for symbol in symbols]
    if not 1 <= len(symbols) <= 8 or len(set(symbols)) != len(symbols):
        raise ValueError("Use 1..8 distinct supported stock identities")
    for symbol in symbols:
        stock_board(symbol)
    if recover_after:
        if (Path(recover_after) / "bounded_recovery.json").exists():
            raise ValueError("A bounded recovery has already been reserved; no repeated retries")
        old = json.loads((Path(recover_after) / "result.json").read_text(encoding="utf-8"))
        if (old.get("source") != ENDPOINT or old.get("status") != "failed_source_request"
                or old.get("entries") or old.get("bounded_recovery_of")
                or old.get("symbols") != symbols or (old.get("start"), old.get("end")) != (start, end)
                or not old.get("error", "").startswith(("ReadTimeout:", "ConnectTimeout:"))):
            raise ValueError("Only one recorded recovery of the matching empty timeout attempt is supported")
    current_day = date.today()
    first_day = date.fromisoformat(start)
    history_limit = min(1000, (current_day.year - first_day.year) * 4
                        + (current_day.month - 1) // 3 - (first_day.month - 1) // 3 + 2)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/alternate_financials" / identifier
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    shutil.copy2(__file__, root / "implementation.py")
    result = {"id": identifier, "status": "collecting", "source": ENDPOINT, "source_code": SOURCE_CODE,
              "symbols": symbols, "start": start, "end": end, "started_at": datetime.now().astimezone().isoformat(),
              "entries": [], "history_query_limit": history_limit,
              "bounded_recovery_of": str(Path(recover_after).resolve()) if recover_after else None,
              "execution_admitted": False, "limits": LIMITS}
    write_json(root / "result.json", result)
    if recover_after:
        with (Path(recover_after) / "bounded_recovery.json").open("x", encoding="utf-8") as receipt:
            json.dump({"recovery_root": str(root), "reserved_at": result["started_at"]}, receipt)
    frames, report_frames = [], []
    current = None
    try:
        for symbol in symbols:
            source_symbol = ("sh" if symbol.endswith(".SSE") else "sz") + symbol[:6]
            for statement in STATEMENTS:
                current = symbol + "_" + statement
                params = {"paperCode": source_symbol, "source": statement, "type": "0", "page": "1", "num": str(history_limit)}
                response = request_get(ENDPOINT, params=params, timeout=(12, 15))
                (root / "raw" / (current + ".bin")).write_bytes(response.content)
                response.raise_for_status()
                frame, reports, entry = decode_statement(response.content, symbol, statement, start, end)
                entry.update(symbol=symbol, statement=statement, response_url=response.url,
                             received_at=datetime.now().astimezone().isoformat())
                result["entries"].append(entry)
                frames.append(frame)
                report_frames.append(reports)
                write_json(root / "result.json", result)
                print(current + ": " + str(entry["requested_periods"]) + " periods / " + str(entry["item_rows"]) + " items", flush=True)
                if not entry["requested_range_not_truncated"]:
                    raise ValueError("Requested statement range truncated; no silent pagination or retry")
                time.sleep(.25)
        result["status"] = "completed_candidate_statements"
    except Exception as error:
        result.update(status="failed_source_request", failed_query=current, error=f"{type(error).__name__}: {error}")
    items = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    reports = pd.concat(report_frames, ignore_index=True) if report_frames else pd.DataFrame()
    items.to_parquet(root / "statement_items.parquet", index=False)
    reports.to_csv(root / "report_metadata.csv", index=False)
    counts = items.groupby(["symbol", "statement", "report_day", "value_status"], dropna=False).size().reset_index(name="items") if len(items) else pd.DataFrame()
    counts.to_csv(root / "field_coverage.csv", index=False)
    result.update(item_rows=len(items), statement_reports=len(reports), finished_at=datetime.now().astimezone().isoformat())
    write_json(root / "result.json", result)
    out = ROOT / "outputs/alternate_financials" / identifier
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "run.json", result | {"artifacts": str(root)})
    lines = ["# Alternate Financial Statement Candidates", "", "Status: " + result["status"] + ". NO_TRADE.",
             f"Stocks: {len(symbols)}; successful statement queries: {len(result['entries'])}/{len(symbols) * 3}; reports: {len(reports)}; items: {len(items)}.",
             "Requested report periods: " + start + " to " + end + ".", "",
             *["- " + value for value in LIMITS], "", "Source implementation: " + SOURCE_CODE,
             "Raw byte receipts, per-item consumer and publication/update metadata: " + str(root)]
    if result.get("error"):
        lines.append("Failure preserved: " + result["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(root), flush=True)
    return root, result


def consume(root):
    root = Path(root).resolve()
    receipt = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if receipt.get("source") != ENDPOINT or receipt.get("execution_admitted") is not False:
        raise ValueError("Existing unadmitted financial statement cache required")
    items = pd.read_parquet(root / "statement_items.parquet")
    reports = pd.read_csv(root / "report_metadata.csv", dtype={"symbol": str, "report_day": str, "publish_day": str})
    if reports.empty:
        raise ValueError("No successfully parsed statements to consume")
    rows, annual_rows = [], []
    for (symbol, day), group in reports.groupby(["symbol", "report_day"], sort=True):
        publish = group.publish_day.dropna()
        rows.append({"symbol": symbol, "report_day": day, "statements_received": ",".join(sorted(group.statement)),
            "missing_statements": ",".join(sorted(set(STATEMENTS) - set(group.statement))),
            "earliest_provider_publish_day": publish.min() if len(publish) else None,
            "latest_provider_publish_day": publish.max() if len(publish) else None,
            "different_statement_publish_dates": publish.nunique() > 1,
            "currency_codes_raw": ",".join(sorted(group.currency_raw.dropna().unique())), "pit_verified": False})
        if not day.endswith("-12-31"):
            continue
        for field, (statement, key) in DENOMINATOR_FIELDS.items():
            candidates = items.loc[items.symbol.eq(symbol) & items.report_day.eq(day)
                                   & items.statement.eq(statement) & items.item_field.eq(key)]
            selected = candidates.iloc[0] if len(candidates) == 1 else None
            row = {"symbol": symbol, "report_day": day, "field": field, "statement": statement, "item_field": key,
                   "value_numeric_unscaled": None, "status": "statement_not_retrieved" if statement not in set(group.statement) else "field_absent",
                   "units_verified": False, "period_semantics_verified": False, "pit_verified": False}
            if len(candidates) > 1:
                row["status"] = "duplicate_field_unresolved"
            elif selected is not None:
                row.update(status=selected.value_status, value_numeric_unscaled=selected.value_numeric_unscaled,
                           item_title=selected.item_title, publish_day=selected.publish_day,
                           provider_update_time=selected.provider_update_time,
                           report_type_raw=selected.report_type_raw, currency_raw=selected.currency_raw)
            annual_rows.append(row)
        annual_rows.append({"symbol": symbol, "report_day": day, "field": "operating_cashflow", "statement": "llb",
            "status": "statement_not_retrieved" if "llb" not in set(group.statement) else "field_mapping_not_verified",
            "value_numeric_unscaled": None, "units_verified": False, "period_semantics_verified": False, "pit_verified": False})
    periods, fields = pd.DataFrame(rows), pd.DataFrame(annual_rows)
    periods.to_csv(root / "statement_period_comparison.csv", index=False)
    fields.to_csv(root / "annual_denominator_fields.csv", index=False)
    summary = {"source_status": receipt["status"], "successful_source_queries": len(receipt["entries"]),
        "stocks_with_statements": int(reports.symbol.nunique()), "statement_reports": len(reports), "item_rows": len(items),
        "observed_periods": len(periods), "periods_with_different_publish_dates": int(periods.different_statement_publish_dates.sum()),
        "periods_missing_cashflow_statement": int(periods.missing_statements.str.contains("llb").sum()),
        "annual_field_slots": len(fields), "annual_numeric_field_slots": int(fields.status.eq("observed_numeric").sum()) if len(fields) else 0,
        "execution_admitted": False, "units_verified": False, "pit_verified": False, "limits": LIMITS}
    write_json(root / "denominator_consumer.json", summary)
    implementation = root / "consumer_implementation.py"
    if not implementation.exists():
        shutil.copy2(__file__, implementation)
    out = ROOT / "outputs/alternate_financials" / receipt["id"]
    lines = ["# Alternate Financial Statement Candidates", "", "Source status: " + receipt["status"] + ". NO_TRADE.",
        f"Successfully retrieved stocks: {summary['stocks_with_statements']}/{len(receipt['symbols'])}; statement queries: {len(receipt['entries'])}/{len(receipt['symbols']) * 3}; reports: {len(reports)}; items: {len(items)}.",
        f"Observed periods: {len(periods)}; differing provider publication dates: {summary['periods_with_different_publish_dates']}; periods missing cashflow statement: {summary['periods_missing_cashflow_statement']}.",
        f"Annual denominator/profit slots: {len(fields)}; observed unscaled numeric slots: {summary['annual_numeric_field_slots']}. This is not an annual quality factor.", "",
        "Provider publication differences are unresolved metadata, not evidence of the issuer's true original disclosure chronology.",
        "No future ratio, primary-unit verification, revision-safe PIT, complete source or account admission is claimed.", "",
        *["- " + value for value in LIMITS], "", "Source implementation: " + SOURCE_CODE,
        "Per-field data and missing states: " + str(root / "annual_denominator_fields.csv"),
        "Cross-statement dates: " + str(root / "statement_period_comparison.csv")]
    if receipt.get("error"):
        lines.append("Source failure preserved: " + receipt["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(out / "run.json", receipt | {"artifacts": str(root), "denominator_consumer": summary})
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end")
    parser.add_argument("--recover-after", type=Path)
    parser.add_argument("--consume", type=Path)
    args = parser.parse_args(argv)
    if args.consume:
        if any((args.symbols, args.end, args.recover_after)):
            parser.error("Offline consumer uses the saved source scope, not new requests")
        consume(args.consume)
        return 0
    if not args.symbols or not args.end:
        parser.error("Collection needs symbols and end date")
    _, result = collect(args.symbols.split(","), args.start, args.end, recover_after=args.recover_after)
    return 1 if result["status"] == "failed_source_request" else 0


if __name__ == "__main__":
    raise SystemExit(main())
