"""Primary annual fields and source reconciliation for the actual financial consumer."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import uuid

import pandas as pd
import requests

from ..settings import DATA_ROOT, ROOT
from .alternate_financials import ENDPOINT as FINANCIAL_ENDPOINT
from .experiment import write_json


PDF_URL = "https://www.catl.com/en/uploads/1/file/public/202603/20260311182739_3924pj798y.pdf"
RELEASE_URL = "https://www.catl.com/en/news/6773.html"
LIMITS = [
    "Only the reviewed3007502025 English consolidated annual-report profile is supported",
    "Primary current-year fields are annual/consolidated/CNY; comparisons do not certify global provider units",
    "Issuer states the original report release day2026-03-10; this translated document's exact first availability is unknown",
    "Available-to-this-system time is actual retrieval, not a backdated historical financial timestamp",
    "Prior-year comparatives are this2025 publication's version, not certified original2024 PIT values",
    "Saved source publication/revision differences remain unresolved; provider values and dates are not overwritten",
    "No factor ranking, ratio construction, account replay, live broker activity or strategy promotion; NO_TRADE",
]


def reconcile(root):
    root = Path(root).resolve()
    receipt = json.loads((root / "result.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "extracted/facts.json").read_text(encoding="utf-8"))
    if evidence["profile"] != "catl_2025_english_consolidated_v1" or receipt["source_pdf"] != PDF_URL:
        raise ValueError("Reviewed primary report profile required")
    provider = Path(receipt["provider_cache"])
    source = json.loads((provider / "result.json").read_text(encoding="utf-8"))
    if source.get("source") != FINANCIAL_ENDPOINT or source.get("execution_admitted") is not False:
        raise ValueError("Existing unadmitted financial candidates required")
    candidates = pd.read_csv(provider / "annual_denominator_fields.csv", dtype={"symbol": str, "report_day": str})
    rows = []
    for fact in evidence["facts"]:
        selected = candidates.loc[candidates.symbol.eq(fact["symbol"]) & candidates.report_day.eq(fact["report_day"])
                                  & candidates.field.eq(fact["field"])]
        if len(selected) != 1:
            raise ValueError("Unique saved provider annual field slot required")
        value = selected.iloc[0]
        observed = value.status == "observed_numeric" and pd.notna(value.value_numeric_unscaled)
        gap = float(value.value_numeric_unscaled) - fact["value_cny"] if observed else None
        rows.append(fact | {"available_to_system_at": receipt["received_at"], "source_url": PDF_URL,
            "provider_value_unscaled": float(value.value_numeric_unscaled) if observed else None,
            "provider_field_status": value.status, "provider_publish_day": value.get("publish_day"),
            "provider_absolute_gap_cny": abs(gap) if gap is not None else None,
            "reconciliation": "exact_same_value_as_cny" if gap == 0 else "provider_conflict" if observed else "primary_only_provider_unavailable",
            "units_reconciled_for_this_exact_field": gap == 0, "pit_verified": False, "execution_admitted": False})
    pd.DataFrame(rows).to_csv(root / "annual_inputs.csv", index=False)
    receipt.update(status="completed_primary_input_consumer", fields=len(rows),
        provider_exact_fields=sum(row["reconciliation"] == "exact_same_value_as_cny" for row in rows),
        provider_conflicts=sum(row["reconciliation"] == "provider_conflict" for row in rows),
        primary_only_fields=sum(row["reconciliation"] == "primary_only_provider_unavailable" for row in rows),
        execution_admitted=False, pit_verified=False, limits=LIMITS)
    write_json(root / "result.json", receipt)
    out = ROOT / "outputs/issuer_annual_inputs" / receipt["id"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", receipt | {"artifacts": str(root)})
    lines = ["# Primary Annual Financial Input Consumer", "", "Status: " + receipt["status"] + ". NO_TRADE.",
        f"3007502025 annual/consolidated fields: {len(rows)}; exact saved-provider CNY matches: {receipt['provider_exact_fields']}; conflicts: {receipt['provider_conflicts']}; primary-only: {receipt['primary_only_fields']}.",
        "Primary cashflow is sourced from the issuer, not a repaired or retried Sina response.", "",
        "|Field|Issuer CNY|Provider status|Comparison|PDF page index|", "|---|---:|---|---|---:|"]
    lines += [f"|{row['field']}|{row['value_cny']}|{row['provider_field_status']}|{row['reconciliation']}|{row['pdf_page_index']}|" for row in rows]
    lines += ["", *["- " + value for value in LIMITS], "", "Issuer report: " + PDF_URL,
              "Issuer original release announcement: " + RELEASE_URL, "Actual consumer: " + str(root / "annual_inputs.csv")]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("id", "status", "fields", "provider_exact_fields", "provider_conflicts", "primary_only_fields")}), flush=True)
    return receipt


def collect(provider_cache, pdf_python):
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/issuer_annual_inputs" / identifier
    root.mkdir(parents=True, exist_ok=False)
    extractor = Path(__file__).with_name("extract_catl_annual.py")
    shutil.copy2(__file__, root / "implementation.py")
    shutil.copy2(extractor, root / "extractor.py")
    receipt = {"id": identifier, "source_pdf": PDF_URL, "source_original_release": RELEASE_URL,
               "original_release_day_reported_by_issuer": "2026-03-10", "document_first_availability_verified": False,
               "provider_cache": str(Path(provider_cache).resolve()), "status": "collecting", "execution_admitted": False}
    write_json(root / "result.json", receipt)
    try:
        response = requests.get(PDF_URL, timeout=(10, 45))
        (root / "issuer_report.pdf").write_bytes(response.content)
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise ValueError("Issuer response is not a PDF")
        receipt.update(received_at=datetime.now().astimezone().isoformat(), response_url=response.url, pdf_bytes=len(response.content))
        write_json(root / "result.json", receipt)
        process = subprocess.run([str(pdf_python), str(extractor), "--pdf", str(root / "issuer_report.pdf"),
                                  "--output", str(root / "extracted")], capture_output=True, text=True,
                                 encoding="utf-8", timeout=180)
        if process.returncode:
            raise ValueError("Primary table extractor failed: " + process.stderr[-2500:])
        receipt["status"] = "primary_extracted"
        write_json(root / "result.json", receipt)
        receipt = reconcile(root)
    except Exception as error:
        receipt.update(status="failed_primary_input", error=f"{type(error).__name__}: {error}")
        write_json(root / "result.json", receipt)
        print(receipt["error"], flush=True)
    print(str(root), flush=True)
    return root, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-cache", type=Path)
    parser.add_argument("--pdf-python", type=Path)
    parser.add_argument("--consume", type=Path)
    args = parser.parse_args(argv)
    if args.consume:
        reconcile(args.consume)
        return 0
    if not args.provider_cache or not args.pdf_python:
        parser.error("Collection needs the existing provider cache and bundled PDF interpreter")
    _, result = collect(args.provider_cache, args.pdf_python)
    return 1 if result["status"] == "failed_primary_input" else 0


if __name__ == "__main__":
    raise SystemExit(main())
