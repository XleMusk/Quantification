"""Bounded market-wide annual statements, retained separately from admitted financials."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from ..worker import lock
from .experiment import write_json
from .fetch_receipts import make_receipt


ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_CODE = "https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_report_em.py"
REPORTS = {"balance": "RPT_DMSK_FN_BALANCE", "income": "RPT_DMSK_FN_INCOME",
           "cashflow": "RPT_DMSK_FN_CASHFLOW"}
PAGE_SIZE = 500
LIMITS = [
    "Retrieval-time public SH/SZ statement candidates; the upstream filter excludes Beijing",
    "Actual original field names, values, publication metadata and raw response bytes are retained",
    "Annual period is fixed; source publication dates do not certify historical version-safe PIT",
    "No guessed unit conversion, missing-as-zero, new ratios, factor ranking or old-pool overwrite",
    "Coverage and accounting identity checks do not independently certify every issuer or unit",
    "No account replay, live broker activity, financial blacklist bypass or automatic failed-request retry; NO_TRADE",
]


def decode_page(content, period, page):
    payload = json.loads(content.decode("utf-8-sig"))
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("Financial source rejected request: " + str(payload.get("message")))
    result = payload["result"]
    count, pages = int(result["count"]), int(result["pages"])
    records = result.get("data")
    if not 0 <= count <= 50000 or not 0 <= pages <= 100:
        raise ValueError("Unexpected market-wide pagination size")
    if count == 0:
        if records not in ([], None) or page != 1:
            raise ValueError("Empty-period metadata conflicts with rows/page")
        return pd.DataFrame(), {"count": 0, "pages": 0, "rows": 0}
    if pages != (count + PAGE_SIZE - 1) // PAGE_SIZE or not 1 <= page <= pages:
        raise ValueError("Financial page count does not match fixed page size")
    expected = min(PAGE_SIZE, count - (page - 1) * PAGE_SIZE)
    if not isinstance(records, list) or len(records) != expected:
        raise ValueError("Financial page is incomplete")
    frame = pd.DataFrame(records)
    for key in ("SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE"):
        if key not in frame or frame[key].isna().any():
            raise ValueError("Required original financial identity/date absent: " + key)
    if not frame.SECURITY_CODE.map(lambda value: isinstance(value, str) and len(value) == 6 and value.isdigit()).all():
        raise ValueError("Financial source security identity changed")
    if not pd.to_datetime(frame.REPORT_DATE, errors="raise").dt.strftime("%Y-%m-%d").eq(period).all():
        raise ValueError("Source returned an unrequested financial period")
    if frame.SECURITY_CODE.duplicated().any():
        raise ValueError("Duplicate securities in financial page")
    return frame, {"count": count, "pages": pages, "rows": len(frame)}


def create(periods):
    if not periods or len(periods) > 4 or len(set(periods)) != len(periods):
        raise ValueError("Use one to four distinct annual periods")
    for period in periods:
        parsed = date.fromisoformat(period)
        if parsed.isoformat() != period or not period.endswith("-12-31") or parsed > date.today():
            raise ValueError("Past canonical annual periods required")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/bulk_annual_financials" / identifier
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    (root / "pages").mkdir()
    shutil.copy2(__file__, root / "implementation.py")
    write_json(root / "plan.json", {"id": identifier, "source": ENDPOINT, "source_code": SOURCE_CODE,
        "created_at": datetime.now().astimezone().isoformat(), "periods": periods,
        "reports": REPORTS, "page_size": PAGE_SIZE, "execution_admitted": False, "limits": LIMITS})
    write_json(root / "result.json", {"id": identifier, "status": "prepared", "groups": {},
                                    "entries": {}, "execution_admitted": False})
    return root


def next_page(plan, result):
    for period in plan["periods"]:
        for report in plan["reports"]:
            key = period + "_" + report
            group = result["groups"].get(key)
            if group is None:
                return period, report, 1
            for page in range(1, group["pages"] + 1):
                if key + "_" + str(page) not in result["entries"]:
                    return period, report, page
    return None


def consume(root):
    root = Path(root).resolve()
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    rows = []
    for period in plan["periods"]:
        for report in plan["reports"]:
            key = period + "_" + report
            group = result["groups"].get(key)
            entries = [entry for entry in result["entries"].values()
                       if entry["period"] == period and entry["report"] == report]
            frames = [pd.read_parquet(root / entry["file"]).assign(
                available_to_system_at=entry["received_at"], source_page=entry["page"])
                      for entry in entries]
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            if len(frame) and frame.SECURITY_CODE.duplicated().any():
                raise ValueError("Pagination shifted or duplicated securities; candidate union rejected")
            complete = group is not None and len(entries) == (group["pages"] or 1)
            if complete and len(frame) != group["count"]:
                raise ValueError("Completed financial group does not match provider count")
            if len(frame):
                frame.to_parquet(root / (key + ".parquet"), index=False)
            rows.append({"period": period, "report": report, "rows": len(frame),
                         "pages_cached": len(entries), "pages_expected": group["pages"] if group else None,
                         "source_rows_expected": group["count"] if group else None,
                         "status": "empty_source_period" if complete and not len(frame) else
                                   "complete_source_period" if complete else "partial" if entries else "not_queried",
                         "units_verified": False, "pit_verified": False, "execution_admitted": False})
    coverage = pd.DataFrame(rows)
    coverage.to_csv(root / "statement_coverage.csv", index=False)
    summary = {"groups": len(rows), "complete_groups": sum(row["status"] == "complete_source_period" for row in rows),
               "cached_pages": len(result["entries"]), "statement_rows": int(coverage.rows.sum()),
               "execution_admitted": False, "pit_verified": False, "units_verified": False}
    write_json(root / "consumer.json", summary)
    out = ROOT / "outputs/bulk_annual_financials" / plan["id"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", result | {"artifacts": str(root), "consumer": summary})
    lines = ["# Bulk Annual Financial Statement Candidates", "", "Status: " + result["status"] + ". NO_TRADE.",
        "|Annual period|Statement|Rows|Cached pages|Expected pages|Source rows|Status|",
        "|---|---|---:|---:|---:|---:|---|"]
    lines += ["|" + "|".join(str(row[column]) for column in
              ("period", "report", "rows", "pages_cached", "pages_expected", "source_rows_expected", "status")) + "|" for row in rows]
    lines += ["", *["- " + value for value in LIMITS], "", "Upstream implementation: " + SOURCE_CODE,
              "Actual statement consumers: " + str(root), "Error: " + str(result.get("error"))]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return summary


def audit_receipts(root):
    """Re-verify cached raw pages without issuing a network request."""

    root = Path(root).resolve()
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    entries, groups = [], {}
    for key, entry in sorted(result.get("entries", {}).items()):
        raw_path = (root / entry["raw_file"]).resolve()
        raw_path.relative_to(root)
        if not raw_path.is_file():
            raise ValueError(f"Cached raw financial page is missing: {entry['raw_file']}")
        content = raw_path.read_bytes()
        frame, meta = decode_page(content, entry["period"], int(entry["page"]))
        if len(frame) != entry["rows"]:
            raise ValueError(f"Cached raw/page row count differs: {key}")
        page_id = f"{key}:page:{entry['page']}"
        receipt = make_receipt(
            {"period": entry["period"], "report": entry["report"], "page": int(entry["page"]), "endpoint": ENDPOINT},
            source="bulk_annual_financials", upstream_source=ENDPOINT,
            adapter_version="bulk_annual_financials_v1",
            requested_scope={"period": entry["period"], "report": entry["report"], "page": int(entry["page"])},
            fetched_at=entry["received_at"],
            raw_content_reference={"file": entry["raw_file"], "sha256": sha256(content).hexdigest()},
            schema_fingerprint=sha256("|".join(frame.columns).encode("utf-8")).hexdigest(),
            expected_partitions=[page_id], completed_partitions=[page_id],
            page_receipts=[{"partition": page_id, "rows": len(frame)}],
            completion_state="COMPLETE" if len(frame) else "COMPLETE_EMPTY")
        entries.append({"key": key, "receipt": receipt, "provider_meta": meta})
    for key, group in sorted(result.get("groups", {}).items()):
        page_entries = [entry for entry in result.get("entries", {}).values()
                        if entry["period"] + "_" + entry["report"] == key]
        expected = int(group["pages"] or 1)
        pages = sorted(int(entry["page"]) for entry in page_entries)
        groups[key] = {"expected_pages": expected, "completed_pages": pages,
                       "status": "COMPLETE" if pages == list(range(1, expected + 1)) else "PARTIAL",
                       "expected_rows": int(group["count"]),
                       "observed_rows": sum(int(entry["rows"]) for entry in page_entries)}
    return {"schema": "bulk_annual_financial_receipt_audit_v1", "status": "COMPLETED_READ_ONLY_AUDIT",
            "source_root": str(root), "source_run_id": plan["id"], "entries": entries, "groups": groups,
            "execution_admitted": False, "network_accessed": False,
            "limitations": ["Audit reuses cached raw responses; it does not establish current provider availability.",
                            "Original disclosure identity and historical PIT semantics still require separate review."]}


def collect(root, max_pages=12, request_get=None):
    if not 1 <= max_pages <= 64:
        raise ValueError("Use a bounded one-to64-page batch")
    if request_get is None:
        import requests

        request_get = requests.get
    root = Path(root).resolve()
    with lock(root / "collector.lock"):
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        result = json.loads((root / "result.json").read_text(encoding="utf-8"))
        if plan["source"] != ENDPOINT or plan["reports"] != REPORTS or plan["page_size"] != PAGE_SIZE:
            raise ValueError("Existing fixed public statement plan required")
        if result["status"] == "failed_source_request" or result["execution_admitted"] is not False:
            raise ValueError("Failed/admitted financial candidates cannot be automatically queried")
        result["status"] = "collecting"
        write_json(root / "result.json", result)
        wanted = None
        try:
            for _ in range(max_pages):
                wanted = next_page(plan, result)
                if wanted is None:
                    break
                period, report, page = wanted
                key = period + "_" + report
                params = {"sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
                    "pageSize": str(PAGE_SIZE), "pageNumber": str(page), "reportName": REPORTS[report], "columns": "ALL",
                    "filter": '(SECURITY_TYPE_CODE in ("058001001","058001008"))(TRADE_MARKET_CODE!="069001017")'
                              + "(REPORT_DATE='" + period + "')"}
                response = request_get(ENDPOINT, params=params, timeout=(5, 15))
                raw_name = "raw/" + key + "_" + str(page) + ".json"
                (root / raw_name).write_bytes(response.content)
                response.raise_for_status()
                frame, meta = decode_page(response.content, period, page)
                fetched_at = datetime.now().astimezone().isoformat()
                page_id = f"{key}:page:{page}"
                raw_sha = sha256(response.content).hexdigest()
                page_receipt = make_receipt(
                    {"period": period, "report": report, "page": page, "endpoint": ENDPOINT},
                    source="bulk_annual_financials", upstream_source=ENDPOINT,
                    adapter_version="bulk_annual_financials_v1",
                    requested_scope={"period": period, "report": report, "page": page},
                    fetched_at=fetched_at,
                    raw_content_reference={"file": raw_name, "sha256": raw_sha},
                    schema_fingerprint=sha256("|".join(frame.columns).encode("utf-8")).hexdigest(),
                    expected_partitions=[page_id], completed_partitions=[page_id],
                    page_receipts=[{"partition": page_id, "rows": len(frame)}],
                    completion_state="COMPLETE" if len(frame) else "COMPLETE_EMPTY")
                if key in result["groups"] and result["groups"][key] != {name: meta[name] for name in ("count", "pages")}:
                    raise ValueError("Financial pagination metadata changed; do not merge versions")
                result["groups"][key] = {name: meta[name] for name in ("count", "pages")}
                file_name = "pages/" + key + "_" + str(page) + ".parquet"
                frame.to_parquet(root / file_name, index=False)
                result["entries"][key + "_" + str(page)] = {"period": period, "report": report, "page": page,
                    "received_at": fetched_at, "response_url": response.url,
                    "raw_file": raw_name, "file": file_name, "rows": len(frame),
                    "fetch_receipt": page_receipt}
                write_json(root / "result.json", result)
                print(f"{period} {report}: page={page}/{meta['pages']}, rows={len(frame)}, source_rows={meta['count']}", flush=True)
                time.sleep(.3)
            result["status"] = "completed_candidate_cache" if next_page(plan, result) is None else "partial_candidate_cache"
        except Exception as error:
            result.update(status="failed_source_request", error=f"{type(error).__name__}: {error}",
                          failed_request=list(wanted) if wanted else None)
        result["batch_finished_at"] = datetime.now().astimezone().isoformat()
        write_json(root / "result.json", result)
        consume(root)
    print(str(root), flush=True)
    return root, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--periods", default="2023-12-31,2024-12-31,2025-12-31")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--consume", type=Path)
    parser.add_argument("--audit-receipts", type=Path)
    args = parser.parse_args(argv)
    if args.consume:
        consume(args.consume)
        return 0
    if args.audit_receipts:
        audit = audit_receipts(args.audit_receipts)
        destination = ROOT / "outputs/bulk_annual_financials" / audit["source_run_id"]
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "receipt_audit.json", audit)
        print(str(destination / "receipt_audit.json"), flush=True)
        return 0
    root = args.resume or create(args.periods.split(","))
    _, result = collect(root, args.max_pages)
    return 1 if result["status"] == "failed_source_request" else 0


if __name__ == "__main__":
    raise SystemExit(main())
