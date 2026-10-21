"""Extract the reviewed English CATL2025 consolidated-table profile, not generic PDF facts."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re

import pdfplumber
import pypdfium2 as pdfium


PROFILE = {
    "total_assets": (116, "Consolidated Balance Sheet", r"Total assets"),
    "total_liabilities": (118, "Consolidated Balance Sheet", r"Total liabilities"),
    "total_equity": (119, "Consolidated Balance Sheet", r"Total shareholders' equity"),
    "parent_equity": (119, "Consolidated Balance Sheet", r"Total equity attributable to shareholders of the Company"),
    "monetary_funds": (115, "Consolidated Balance Sheet", r"Bank balances, deposits and cash"),
    "revenue": (124, "Consolidated Income", r"I\. Operating income"),
    "total_net_profit": (125, "Consolidated Income", r"V\. Net profit for the year[^\d]{0,45}"),
    "parent_net_profit": (125, "Consolidated Income", r"Including: Net profit attributable to Shareholders of the Company[^\d]{0,45}"),
    "operating_cashflow": (129, "Consolidated Cash Flow Statement", r"Net cash flows from operating activities"),
}


def normalize(text):
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(text.split())


def extract(pdf, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    facts, pages = [], {}
    with pdfplumber.open(pdf) as document:
        if len(document.pages) != 306:
            raise ValueError("Unsupported annual-report layout; CATL2025 profile requires306 pages")
        for number in sorted({item[0] for item in PROFILE.values()}):
            text = document.pages[number].extract_text()
            if not text:
                raise ValueError("No extractable financial table text")
            pages[number] = normalize(text)
            (output / f"page_{number}.txt").write_text(text, encoding="utf-8")
        for field, (number, scope, label) in PROFILE.items():
            text = pages[number]
            if scope.lower() not in text.lower() or "Unit: RMB'000" not in text:
                raise ValueError(f"Consolidated scope/unit header absent for {field}")
            pattern = label + r"\s+(-?\d[\d,]*(?:\.\d+)?)\s+(-?\d[\d,]*(?:\.\d+)?)"
            found = list(re.finditer(pattern, text, flags=re.IGNORECASE))
            if len(found) != 1:
                raise ValueError(f"Unique current/prior financial cells absent for {field}: {len(found)}")
            values = [Decimal(value.replace(",", "")) for value in found[0].groups()]
            facts.append({"symbol": "300750.SZSE", "report_day": "2025-12-31", "field": field,
                "pdf_page_index": number, "printed_unit": "RMB thousand", "unit_multiplier": 1000,
                "current_year_raw": str(values[0]), "previous_year_comparative_raw": str(values[1]),
                "value_cny": int(values[0] * 1000), "comparative_value_cny": int(values[1] * 1000),
                "consolidated_scope": True, "row_evidence": found[0].group(0)})
    rendered = pdfium.PdfDocument(str(pdf))
    for number in pages:
        page = rendered[number]
        bitmap = page.render(scale=1.7)
        bitmap.to_pil().save(output / f"page_{number}.png")
        bitmap.close()
        page.close()
    rendered.close()
    result = {"profile": "catl_2025_english_consolidated_v1", "pages": 306, "facts": facts,
              "not_generic_issuer_support": True, "pit_verified": False}
    (output / "facts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Extracted {len(facts)} current-year fields with prior-year comparatives", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    extract(args.pdf, args.output)
