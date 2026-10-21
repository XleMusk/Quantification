"""Pin acquired SH/SZ equities or fixed pre-period main-board facts without replacing live feeds."""
import argparse
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .feed import HistoricalFeed
from .rules import holder_distribution_action, holder_restructuring_ex_price, restructuring_ex_price
from .market_dataset import (DEFAULT_ROOT, bar_quality, load_receipt, task_complete,
                             normalize_symbol, tasks_for, write_frame, write_json)

FIXED_SCOPE = "fixed_preperiod_hs300_mainboard_20231229"
INGESTION_SCOPE = "acquired_sh_sz_equities_ingestion_regression_not_strategy_validation"
RESERVE_CORRECTION_FIELDS = ("code", "dividRegistDate", "dividPayDate", "dividStockMarketDate",
                             "dividCashPsBeforeTax", "dividCashPsAfterTax", "dividStocksPs",
                             "dividReserveToStockPs", "dividCashStock")

REVIEWED_IR_NOTICES = {
    "https://announcement.valueonline.cn/20230705/202307051688469340005025192.pdf": {
        "symbol": "300952.SZSE", "source_date": "2023-07-05", "source_notice_id": "2023-054",
        "issuer_page_url": "https://www.hhglove.com/menu/investor.html",
        "issuer_ir_url": "https://300952.ir-online.com.cn/",
    }
}

# One reviewed issuer-hosted document, bound to its original bytes and issuer.
# This admits its provenance only. The reviewed cash-only interpretation does
# not become an explicit numeric reserve-zero statement or historical PIT proof.
REVIEWED_ISSUER_DOCUMENTS = {
    "https://pic.bankofchina.com/bocappd/report/202504/P020250420624888698809.pdf": {
        "symbol": "601988.SSE", "source_date": "2025-04-20",
        "source_notice_id": "\u4e342025-031", "source_channel": "issuer_official_website",
        "source_sha256": "cad43128d472f79061ec4a9318e5f49ed1c22827d1ddb5fe68f2e668ba61bc1e",
    }
}

# A single issuer notice mirrored by a market-document host. Keep this source
# channel explicit so a mirror never becomes a general-purpose evidence feed.
REVIEWED_MIRROR_NOTICES = {
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-9/2026-09-17/12606766.PDF": {
        "symbol": "000538.SZSE",
        "source_date": "2026-09-17",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://stockmc.xueqiu.com/202606/600105_20260610_9HG1.pdf": {
        "symbol": "600105.SSE", "source_date": "2026-06-10",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-6/2026-06-23/12404850.PDF": {
        "symbol": "001203.SZSE", "source_date": "2026-06-23",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-2/2026-02-02/11947028.PDF": {
        "symbol": "002252.SZSE", "source_date": "2026-02-02",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-5/2026-05-15/12319832.PDF": {
        "symbol": "002895.SZSE", "source_date": "2026-05-15",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-09/12379805.PDF": {
        "symbol": "600801.SSE", "source_date": "2026-06-09",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-5/2026-05-30/12362499.PDF": {
        "symbol": "600177.SSE", "source_date": "2026-05-30",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-04/12371703.PDF": {
        "symbol": "601158.SSE", "source_date": "2026-06-04",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-11/12385149.PDF": {
        "symbol": "600295.SSE", "source_date": "2026-06-11",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-10/12382730.PDF": {
        "symbol": "600332.SSE", "source_date": "2026-06-10",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-6/2026-06-09/12380621.PDF": {
        "symbol": "002539.SZSE", "source_date": "2026-06-09",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-12/12387765.PDF": {
        "symbol": "600511.SSE", "source_date": "2026-06-12",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-6/2026-06-22/12402880.PDF": {
        "symbol": "601899.SSE", "source_date": "2026-06-22",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://infonotice.sylapp.cn/LC_NotTextAnnouncement/2026/06/19/835122873022.PDF": {
        "symbol": "600035.SSE", "source_date": "2026-06-19",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-8/2026-08-26/12534077.PDF": {
        "symbol": "002895.SZSE", "source_date": "2026-08-26",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
    "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-9/2026-09-11/12594285.PDF": {
        "symbol": "600236.SSE", "source_date": "2026-09-11",
        "source_channel": "issuer_mirrored_exchange_notice",
    },
}


class _FrameSources(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.sources = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


def official_distribution_notice_date(url):
    patterns = (
        r"https://static\.cninfo\.com\.cn/finalpage/([0-9]{4}-[0-9]{2}-[0-9]{2})/[0-9]+\.[Pp][Dd][Ff]",
        r"https://disc\.static\.szse\.cn/download/disc/disk[0-9]+/finalpage/"
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\.[Pp][Dd][Ff]",
        r"https://static\.sse\.com\.cn/disclosure/listedinfo/announcement/c/new/"
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})/[0-9]{6}_[0-9]{8}_[A-Za-z0-9]+\.[Pp][Dd][Ff]",
        r"https://big5\.sse\.com\.cn/site/cht/www\.sse\.com\.cn/disclosure/"
        r"listedinfo/announcement/c/new/([0-9]{4}-[0-9]{2}-[0-9]{2})/"
        r"[0-9]{6}_[0-9]{8}_[A-Za-z0-9]+\.[Pp][Dd][Ff]",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, url)
        if match:
            return date.fromisoformat(match[1]).isoformat()
    raise ValueError("Literal dated CNinfo or SZSE disclosure PDF required")


def reviewed_cash_notice_source(row, root):
    try:
        source_date = official_distribution_notice_date(row["source_url"])
    except ValueError:
        source_date = None
    if source_date is not None:
        return source_date == row["source_date"]
    issuer_document = REVIEWED_ISSUER_DOCUMENTS.get(row["source_url"])
    if issuer_document is not None:
        return (row.get("kind") == "issuer_confirmed_cash_only_terms"
                and all(row.get(key) == value for key, value in issuer_document.items())
                and row.get("reserve_numeric_zero_explicit_in_notice") is False
                and row.get("complete_cash_only_scheme_projection") is True
                and row.get("pit_verified") is False)
    mirrored = REVIEWED_MIRROR_NOTICES.get(row["source_url"])
    if mirrored is not None:
        return (row.get("source_channel") == mirrored["source_channel"]
                and row.get("symbol") == mirrored["symbol"]
                and row.get("source_date") == mirrored["source_date"])
    # Only a specifically reviewed issuer-published notice can extend the source channel.
    expected = REVIEWED_IR_NOTICES.get(row["source_url"])
    if (expected is None or row["kind"] != "issuer_cash_only_precision_correction"
            or row.get("source_channel") != "issuer_linked_ir_valueonline"
            or any(row.get(key) != value for key, value in expected.items())):
        return False
    documents = {}
    for prefix in ("issuer_page", "issuer_ir"):
        if not all(row.get(prefix + suffix) for suffix in ("_file", "_sha256")):
            return False
        path = (root / row[prefix + "_file"]).resolve()
        path.relative_to(root)
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != row[prefix + "_sha256"]:
            raise ValueError("Reviewed issuer publication context changed")
        documents[prefix] = _FrameSources(raw.decode("utf-8")).sources
    code = row["symbol"].split(".")[0]
    return (any(url.rstrip("/") == expected["issuer_ir_url"].rstrip("/")
                for url in documents["issuer_page"])
            and any(urlparse(url).scheme == "https" and urlparse(url).netloc == "mf.valueonline.cn"
                    and urlparse(url).path == "/ui/irh/index"
                    and parse_qs(urlparse(url).query).get("companyCodeValue") == [code]
                    for url in documents["issuer_ir"]))


def reviewed_plan_scope(row, pages):
    if row["plan_review_status"] == "manual_fulltext_and_pages_reviewed":
        return True
    if row["plan_review_status"] != "manual_distribution_sections_and_pages_reviewed":
        return False
    # A long annual report is reviewed for one named distribution, not unrelated financial statements.
    period = row.get("plan_distribution_period", "")
    total = row.get("plan_source_page_count")
    expected = row["expected_provider_terms"]
    return (row["kind"] == "issuer_confirmed_reserve_zero_term"
            and row.get("plan_document_kind") == "issuer_annual_report"
            and bool(re.fullmatch(r"20[0-9]{2}FY", period))
            and int(period[:4]) == int(row["source_date"][:4]) - 1
            and row.get("plan_review_scope") == "issuer_identity_distribution_terms_and_board_approval"
            and row.get("plan_implementation_period") == period
            and expected["dividStocksPs"] != "" and Decimal(expected["dividStocksPs"]) == 0
            and row.get("shareholder_approval_date") is not None
            and date.fromisoformat(row["plan_source_date"]) <= date.fromisoformat(row["shareholder_approval_date"])
                <= date.fromisoformat(row["source_date"])
            and type(total) is int and isinstance(pages, list) and bool(pages)
            and all(type(page) is int and 1 <= page <= total for page in pages))


def reviewed_adjusted_plan_cash(row, cash, known_bonus):
    """Bind a reviewed cash-only treasury exclusion; do not change holder cash."""
    adjustment = row["plan_cash_adjustment"]
    fields = {"basis", "plan_period", "implementation_period", "plan_total_shares", "treasury_shares",
              "participating_shares", "declared_cash_total", "issuer_cash_per_ten", "reference"}
    if not isinstance(adjustment, dict):
        raise ValueError("Invalid reviewed treasury cash adjustment linkage")
    period = str(adjustment.get("plan_period", ""))
    cash_kinds = {"issuer_confirmed_cash_only_terms", "issuer_cash_only_precision_correction"}
    interim = (bool(re.fullmatch(r"[0-9]{4}H1", period)) and period[:4] == row["source_date"][:4]
               and row["kind"] in cash_kinds)
    annual = (bool(re.fullmatch(r"[0-9]{4}FY", period))
              and int(period[:4]) == int(row["source_date"][:4]) - 1
              and row["kind"] in cash_kinds)
    if interim and row["kind"] == "issuer_cash_only_precision_correction":
        if (row.get("approval_basis") != "shareholder_authorized_interim_board"
                or not {"shareholder_authorization_date", "plan_approval_date", "plan_source_date"} <= row.keys()
                or not (date.fromisoformat(row["shareholder_authorization_date"])
                        <= date.fromisoformat(row["plan_approval_date"])
                        <= date.fromisoformat(row["plan_source_date"]) <= date.fromisoformat(row["source_date"]))):
            raise ValueError("Interim treasury precision requires reviewed authorized-board approval")
    if annual:
        if not {"shareholder_approval_date", "plan_source_date", "plan_cash_per_share"} <= row.keys():
            raise ValueError("Annual treasury adjustment requires reviewed plan and shareholder approval")
        if not (date.fromisoformat(row["plan_source_date"]) <= date.fromisoformat(row["shareholder_approval_date"])
                <= date.fromisoformat(row["source_date"])):
            raise ValueError("Annual treasury adjustment approval dates conflict")
    if (set(adjustment) != fields
            or adjustment["basis"] != "fixed_total_cash_treasury_exclusion"
            or not (interim or annual) or known_bonus != 0
            or adjustment["plan_period"] != adjustment["implementation_period"]
            or not isinstance(adjustment["reference"], str) or not adjustment["reference"].strip()
            or any(not isinstance(adjustment[key], str) for key in fields - {
                "basis", "plan_period", "implementation_period", "reference"})):
        raise ValueError("Invalid reviewed treasury cash adjustment linkage")
    total, treasury, participating, declared, per_ten = (
        Decimal(adjustment[key]) for key in ("plan_total_shares", "treasury_shares", "participating_shares",
                                           "declared_cash_total", "issuer_cash_per_ten"))
    approved_cash = Decimal(row["plan_cash_per_share"])
    plan = re.match(r"10\u6d3e([0-9]+(?:\.[0-9]+)?)\u5143", row["expected_provider_terms"]["dividCashStock"])
    amounts = (total, treasury, participating, declared, per_ten, approved_cash, cash)
    if (not all(value.is_finite() and value > 0 for value in amounts)
            or any(value != value.to_integral_value() for value in (total, treasury, participating))
            or treasury >= total or total - treasury != participating
            or declared.as_tuple().exponent != -2 or per_ten.as_tuple().exponent != -6
            or (approved_cash * total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != declared
            or per_ten != cash * 10 or plan is None or Decimal(plan[1]) != per_ten
            # The final notice gives the literal six-decimal per-ten amount, not exact payout equality.
            or not per_ten <= declared * 10 / participating < per_ten + Decimal("0.000001")):
        raise ValueError("Reviewed treasury cash arithmetic conflicts with literal issuer/provider terms")
    return approved_cash


def load_action_resolutions(root, symbols):
    root = Path(root).resolve()
    path = root / "supplements/provider_action_resolutions.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(rows, list):
        raise ValueError("Provider action resolutions must be a reviewed list")
    result = []
    for row in rows:
        if row["symbol"] not in symbols:
            continue
        if row["kind"] in {"issuer_confirmed_cash_only_terms", "issuer_cash_only_precision_correction",
                           "issuer_confirmed_reserve_zero_term"}:
            precision = row["kind"] == "issuer_cash_only_precision_correction"
            reserve_only = row["kind"] == "issuer_confirmed_reserve_zero_term"
            expected = row.get("expected_provider_terms")
            if (not isinstance(expected, dict) or set(expected) != set(RESERVE_CORRECTION_FIELDS)
                    or not all(isinstance(value, str) for value in expected.values())):
                raise ValueError("Invalid issuer cash-only expected source terms")
            known_bonus = Decimal(expected["dividStocksPs"] or "0")
            positive_bonus = reserve_only and known_bonus.is_finite() and known_bonus > 0
            # A positive known bonus needs its approved plan and explicit taxable basis.
            if reserve_only and (expected["dividReserveToStockPs"] != ""
                    or expected["dividStocksPs"] == "" or not known_bonus.is_finite() or known_bonus < 0
                    or row.get("confirmed_fields") != ["dividReserveToStockPs"]
                    or row.get("known_terms_basis") != "provider_cash_and_bonus_preserved"
                    or (not positive_bonus and "plan_bonus_per_share" in row)):
                raise ValueError("Reserve-only completion requires unchanged known cash/bonus and blank reserve")
            dates = [date.fromisoformat(row[field]) for field in
                     ("source_date", "dividRegistDate", "dividOperateDate", "dividPayDate")]
            cash, original = Decimal(row["dividCashPsBeforeTax"]), Decimal(expected["dividCashPsBeforeTax"])
            valid_cash = cash.is_finite() and original.is_finite() and cash > 0 and original > 0
            if precision and valid_cash:
                per_ten = Decimal(row["issuer_cash_per_ten"])
                plan = re.match(r"10\u6d3e([0-9]+(?:\.[0-9]+)?)\u5143", expected["dividCashStock"])
                quantum = Decimal(1).scaleb(original.as_tuple().exponent)
                annual_precision = False
                if quantum == Decimal("0.00001") and "plan_cash_adjustment" in row:
                    # Five-decimal display rounding needs exact fixed-total arithmetic plus its reviewed annual plan.
                    reviewed_adjusted_plan_cash(row, cash, known_bonus)
                    annual_precision = str(row["plan_cash_adjustment"]["plan_period"]).endswith("FY")
                # A coarse displayed share amount can still differ by just one seventh decimal.
                precision_supported = (quantum <= Decimal("0.000001") or annual_precision or
                    (quantum == Decimal("0.001") and per_ten.is_finite() and per_ten.as_tuple().exponent <= -6
                     and abs(cash - original) <= Decimal("0.0000001")))
                valid_cash = (per_ten.is_finite() and per_ten == cash * 10
                              and plan is not None and Decimal(plan[1]) == per_ten
                              and precision_supported and 0 < abs(cash - original) < quantum
                              and original in {cash.quantize(quantum, rounding=ROUND_DOWN),
                                               cash.quantize(quantum, rounding=ROUND_HALF_UP)})
            elif valid_cash:
                valid_cash = cash == original
            source = (root / row["source_file"]).resolve()
            source.relative_to(root)
            shares = ("dividStocksPs", "dividReserveToStockPs")
            valid_shares = (Decimal(row["dividStocksPs"]) == known_bonus
                            and row["dividReserveToStockPs"] == "0") if positive_bonus else all(
                                row[field] == "0" and (expected[field] == "" or Decimal(expected[field]) == 0)
                                for field in shares)
            listing = row["dividStockMarketDate"]
            valid_listing = bool(listing) and date.fromisoformat(listing) >= dates[2] if positive_bonus else listing == ""
            if (row.get("review_status") != "manual_fulltext_and_pages_reviewed"
                    or dates != sorted(dates) or dates[1] >= dates[2]
                    or any(expected[field] != row[field] for field in
                           ("code", "dividRegistDate", "dividPayDate", "dividStockMarketDate"))
                    or not valid_listing
                    or not valid_cash
                    or not any(expected[field] == "" for field in shares)
                    or not valid_shares
                    or normalize_symbol(row["symbol"]).baostock_code != row["code"]
                    or not reviewed_cash_notice_source(row, root)
                    or not source.is_file()):
                raise ValueError("Invalid issuer cash-only completion")
            raw = source.read_bytes()
            if not raw.startswith(b"%PDF-") or sha256(raw).hexdigest() != row["source_sha256"]:
                raise ValueError("Issuer cash-only source hash/PDF changed")
            plan_fields = {"plan_source_file", "plan_source_url", "plan_source_sha256", "plan_source_date",
                           "plan_source_pages", "plan_review_status", "plan_symbol", "plan_approval_date",
                           "plan_reference", "plan_cash_per_share", "plan_bonus_per_share", "plan_reserve_per_share"}
            if reserve_only and not positive_bonus:
                plan_fields.remove("plan_bonus_per_share")
            if positive_bonus:
                if not plan_fields <= row.keys():
                    raise ValueError("Positive bonus requires complete reviewed approved-plan linkage")
                basis = Decimal(row.get("bonus_taxable_cash_ps", "NaN"))
                nominal = Decimal(row.get("bonus_nominal_value_ps", "NaN"))
                approved = date.fromisoformat(row.get("shareholder_approval_date", ""))
                if (not basis.is_finite() or not nominal.is_finite() or basis <= 0 or nominal <= 0
                        or basis != known_bonus * nominal
                        or not date.fromisoformat(row["plan_source_date"]) <= approved <= dates[0]
                        or not isinstance(row.get("bonus_tax_basis_reference"), str)
                        or not row["bonus_tax_basis_reference"].strip()):
                    raise ValueError("Positive bonus requires explicit reviewed taxable basis and final approval")
            if "plan_cash_adjustment" in row and not plan_fields <= row.keys():
                raise ValueError("Cash adjustment requires complete reviewed approved-plan linkage")
            if plan_fields.intersection(row):
                if not plan_fields <= row.keys():
                    raise ValueError("Complete reviewed approved-plan linkage required")
                plan = (root / row["plan_source_file"]).resolve()
                plan.relative_to(root)
                approval, publication = (date.fromisoformat(row[field]) for field in
                                         ("plan_approval_date", "plan_source_date"))
                term_fields = (("plan_cash_per_share", "plan_reserve_per_share") if reserve_only and not positive_bonus else
                               ("plan_cash_per_share", "plan_bonus_per_share", "plan_reserve_per_share"))
                terms = [Decimal(row[field]) for field in term_fields]
                approved_cash = (reviewed_adjusted_plan_cash(row, cash, known_bonus)
                                 if "plan_cash_adjustment" in row else cash)
                expected_terms = ([cash, known_bonus, Decimal(0)] if positive_bonus else
                                  [cash, Decimal(0)] if reserve_only else [approved_cash, Decimal(0), Decimal(0)])
                pages = row["plan_source_pages"]
                try:
                    plan_source_date = official_distribution_notice_date(row["plan_source_url"])
                except ValueError:
                    plan_source_date = None
                if ((precision and "plan_cash_adjustment" not in row) or row["plan_symbol"] != row["symbol"]
                        or not reviewed_plan_scope(row, pages)
                        or not approval <= publication <= dates[0]
                        or not isinstance(row["plan_reference"], str) or not row["plan_reference"].strip()
                        or not all(value.is_finite() for value in terms) or terms != expected_terms
                        or not isinstance(pages, list) or not pages
                        or any(type(page) is not int or page < 1 for page in pages) or pages != sorted(set(pages))
                        or plan_source_date != row["plan_source_date"]
                        or not plan.is_file() or plan == source):
                    raise ValueError("Invalid reviewed approved-plan linkage")
                plan_raw = plan.read_bytes()
                if not plan_raw.startswith(b"%PDF-") or sha256(plan_raw).hexdigest() != row["plan_source_sha256"]:
                    raise ValueError("Approved-plan source hash/PDF changed")
                row = {**row, "plan_source_file": plan.relative_to(root).as_posix()}
            result.append({**row, "source_file": source.relative_to(root).as_posix()})
            continue
        if row["kind"] in {"issuer_distribution_correction", "issuer_combined_cash_correction"}:
            combined_cash = row["kind"] == "issuer_combined_cash_correction"
            expected, selected = row.get("expected_provider_rows"), row.get("selected_provider_terms")
            if (not isinstance(expected, list) or len(expected) != 2 or not isinstance(selected, dict)
                    or any(not isinstance(item, dict) or set(item) != set(RESERVE_CORRECTION_FIELDS)
                           or not all(isinstance(value, str) for value in item.values()) for item in expected)
                    or expected[0] == expected[1] or selected not in expected):
                raise ValueError("Invalid issuer distribution-correction source multiset")
            ex = date.fromisoformat(row["dividOperateDate"])
            dates = [date.fromisoformat(row[field]) for field in
                     (("dividRegistDate", "dividPayDate") if combined_cash else
                      ("dividRegistDate", "dividPayDate", "dividStockMarketDate"))]
            amounts = [Decimal(row[field]) for field in
                       ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")]
            before = [Decimal(item[field] or "0") for item in expected for field in
                      ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")]
            source = (root / row["source_file"]).resolve()
            source.relative_to(root)
            if (any(item[field] != row[field] for item in expected for field in
                    ("code", "dividRegistDate", "dividPayDate", "dividStockMarketDate"))
                    or dates[0] >= ex or any(day < ex for day in dates[1:])
                    or date.fromisoformat(row["source_date"]) > ex
                    or not all(value.is_finite() and value >= 0 for value in amounts + before)
                    or amounts[0] <= 0 or amounts[1] != 0
                    or (amounts[2] != 0 if combined_cash else not 0 < amounts[2] <= 1)
                    or any(Decimal(item["dividStocksPs"]) != 0 for item in expected)
                    or (combined_cash and (row["dividStockMarketDate"] != ""
                        or date.fromisoformat(row["source_date"]) > dates[0]
                        or any(Decimal(item["dividReserveToStockPs"] or "0") != 0
                               or Decimal(item["dividCashPsBeforeTax"]) <= 0 for item in expected)))
                    or normalize_symbol(row["symbol"]).baostock_code != row["code"]
                    or not row["source_url"].startswith("https://") or not source.is_file()):
                raise ValueError("Invalid issuer distribution correction")
            raw = source.read_bytes()
            if not raw.startswith(b"%PDF-") or sha256(raw).hexdigest() != row["source_sha256"]:
                raise ValueError("Issuer distribution-correction source hash/PDF changed")
            result.append({**row, "source_file": source.relative_to(root).as_posix()})
            continue
        if row["kind"] == "issuer_reserve_bonus_correction":
            expected = row.get("expected_provider_terms")
            dates = [date.fromisoformat(row[field]) for field in
                     ("source_date", "dividRegistDate", "dividOperateDate", "dividStockMarketDate")]
            bonus = Decimal(row["dividReserveToStockPs"])
            source = (root / row["source_file"]).resolve()
            source.relative_to(root)
            explicit_no_cash = (isinstance(expected, dict)
                                and expected.get("dividCashPsBeforeTax") == ""
                                and row.get("no_cash_confirmed") is True)
            explicit_no_cash_reserve = (explicit_no_cash
                                        and isinstance(expected.get("dividReserveToStockPs"), str)
                                        and expected["dividReserveToStockPs"] != ""
                                        and Decimal(expected["dividReserveToStockPs"]).is_finite()
                                        and 0 < Decimal(expected["dividReserveToStockPs"]) <= 1
                                        and Decimal(row["dividReserveToStockPs"]) ==
                                        Decimal(expected["dividReserveToStockPs"]))
            expected_cash_valid = explicit_no_cash or (
                isinstance(expected, dict)
                and expected.get("dividCashPsBeforeTax", "") != ""
                and Decimal(expected["dividCashPsBeforeTax"]).is_finite()
                and Decimal(expected["dividCashPsBeforeTax"]) > 0)
            if (not isinstance(expected, dict) or set(expected) != set(RESERVE_CORRECTION_FIELDS)
                    or not all(isinstance(value, str) for value in expected.values())
                    or any(expected[field] != row[field] for field in
                           ("code", "dividRegistDate", "dividPayDate", "dividStockMarketDate"))
                    or not expected_cash_valid
                    or (expected["dividReserveToStockPs"] != "" and not explicit_no_cash_reserve)
                    or Decimal(expected["dividStocksPs"]) != 0
                    or expected["dividCashPsAfterTax"] != ""
                    or row["dividPayDate"] != "" or row["dividCashPsBeforeTax"] != "0"
                    or row["dividStocksPs"] != "0" or not bonus.is_finite() or not 0 < bonus <= 1
                    or (explicit_no_cash_reserve and
                        bonus != Decimal(expected["dividReserveToStockPs"]))
                    or dates != sorted(dates) or dates[1] >= dates[2]
                    or normalize_symbol(row["symbol"]).baostock_code != row["code"]
                    or not row["source_url"].startswith("https://") or not source.is_file()):
                raise ValueError("Invalid issuer reserve-bonus correction")
            raw = source.read_bytes()
            if not raw.startswith(b"%PDF-") or sha256(raw).hexdigest() != row["source_sha256"]:
                raise ValueError("Issuer reserve-bonus source hash/PDF changed")
            result.append({**row, "source_file": source.relative_to(root).as_posix()})
            continue
        dates = [date.fromisoformat(row[field]) for field in
                 ("source_date", "dividRegistDate", "dividOperateDate", "dividPayDate")]
        amounts = [Decimal(value) for value in row["expected_provider_cash_amounts"]]
        cash = Decimal(row["dividCashPsBeforeTax"])
        source = (root / row["source_file"]).resolve()
        source.relative_to(root)
        combined = row["kind"] == "issuer_combined_cash_event"
        valid_cash = (cash == sum(amounts) if combined else cash in amounts) if cash.is_finite() else False
        no_shares = all(row.get(field) == "0" for field in ("dividStocksPs", "dividReserveToStockPs"))
        if (row["kind"] not in {"issuer_cash_correction", "issuer_combined_cash_event"}
                or dates != sorted(dates) or dates[1] >= dates[2]
                or (len(amounts) < 2 if combined else len(set(amounts)) < 2)
                or any(not value.is_finite() or value <= 0 for value in amounts)
                or not valid_cash or (combined and not no_shares)
                or normalize_symbol(row["symbol"]).baostock_code != row["code"]
                or not row["source_url"].startswith("https://") or not source.is_file()):
            raise ValueError("Invalid issuer cash resolution")
        raw = source.read_bytes()
        if not raw.startswith(b"%PDF-") or sha256(raw).hexdigest() != row["source_sha256"]:
            raise ValueError("Issuer cash-correction source hash/PDF changed")
        result.append({**row, "source_file": source.relative_to(root).as_posix()})
    return result


def reconcile_provider_actions(actions, resolutions=()):
    """Correct only exact documented source terms; combine issuer-confirmed cash."""
    keys = ("symbol", "dividOperateDate")
    dates = ("code", "dividRegistDate", "dividPayDate", "dividStockMarketDate")
    amounts = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
    keep, audit, used, overrides = [], [], set(), {}
    resolved = {(row["symbol"], row["dividOperateDate"]): row for row in resolutions}
    if len(resolved) != len(resolutions):
        raise ValueError("Duplicate issuer cash-correction resolution")
    for (symbol, operation_date), group in actions.groupby(list(keys), sort=False):
        resolution = resolved.get((symbol, operation_date))
        if resolution is not None and resolution["kind"] in {"issuer_distribution_correction", "issuer_combined_cash_correction"}:
            fields = RESERVE_CORRECTION_FIELDS
            expected = resolution["expected_provider_rows"]
            if (not set(fields) <= set(group)
                    or sorted(tuple(item[field] for field in fields) for item in group.to_dict("records"))
                    != sorted(tuple(item[field] for field in fields) for item in expected)):
                raise ValueError(f"Issuer distribution-correction source terms changed: {symbol} {operation_date}")
            selected = resolution["selected_provider_terms"]
            chosen = next(index for index in group.index
                          if all(group.at[index, field] == selected[field] for field in fields))
            keep.append(chosen)
            used.add((symbol, operation_date))
            overrides[chosen] = {**{field: resolution[field] for field in amounts}, "dividCashPsAfterTax": ""}
            audit.append({"symbol": symbol, "dividOperateDate": operation_date,
                          "status": ("issuer_combined_cash_event" if resolution["kind"] == "issuer_combined_cash_correction"
                                     else "issuer_corrected_distribution_event"),
                          "chosen_cache_file": actions.at[chosen, "_cache_file"],
                          "source_rows": group.to_dict("records"), "canonical_overrides": overrides[chosen],
                          "issuer_resolution": resolution})
            continue
        if resolution is not None and resolution["kind"] in {"issuer_reserve_bonus_correction", "issuer_confirmed_cash_only_terms",
                                                            "issuer_cash_only_precision_correction", "issuer_confirmed_reserve_zero_term"}:
            cash_only = resolution["kind"] != "issuer_reserve_bonus_correction"
            precision = resolution["kind"] == "issuer_cash_only_precision_correction"
            if (len(group) != 1 or any(field not in group or group.iloc[0][field] != value
                                      for field, value in resolution["expected_provider_terms"].items())):
                raise ValueError(f"Issuer {'cash-only' if cash_only else 'reserve-bonus'} source terms changed: {symbol} {operation_date}")
            chosen = group.index[0]
            keep.append(chosen)
            used.add((symbol, operation_date))
            overrides[chosen] = {field: resolution[field] for field in amounts
                                 if not cash_only or group.iloc[0][field] == ""
                                 or (precision and field == "dividCashPsBeforeTax")}
            if "bonus_taxable_cash_ps" in resolution:
                overrides[chosen]["bonus_taxable_cash_ps"] = resolution["bonus_taxable_cash_ps"]
            audit.append({"symbol": symbol, "dividOperateDate": operation_date,
                          "status": ("issuer_completed_reserve_zero_term" if resolution["kind"] == "issuer_confirmed_reserve_zero_term" else
                                     "issuer_corrected_cash_only_precision" if precision else
                                     "issuer_completed_cash_only_terms" if cash_only else "issuer_corrected_reserve_bonus_event"),
                          "chosen_cache_file": actions.at[chosen, "_cache_file"],
                          "source_rows": group.to_dict("records"),
                          "canonical_overrides": overrides[chosen], "issuer_resolution": resolution})
            continue
        if len(group) == 1:
            keep.append(group.index[0])
            continue
        if not operation_date or any(group[field].nunique() != 1 for field in dates):
            raise ValueError(f"Conflicting provider action dates/code: {symbol} {operation_date}")
        conflict = any(len({Decimal(value or "0") for value in group[field]}) != 1 for field in amounts)
        combined = resolution is not None and resolution["kind"] == "issuer_combined_cash_event"
        choices = group
        if conflict or combined:
            observed = {Decimal(value or "0") for value in group.dividCashPsBeforeTax}
            if (resolution is None
                    or resolution["kind"] not in {"issuer_cash_correction", "issuer_combined_cash_event"}
                    or any(not group[field].eq(resolution[field]).all() for field in dates[:3])
                    or observed != {Decimal(value) for value in resolution["expected_provider_cash_amounts"]}
                    or any(Decimal(value or "0") != 0 for field in amounts[1:] for value in group[field])):
                raise ValueError(f"Conflicting provider action amounts: {symbol} {operation_date}")
            if combined:
                components = [Decimal(value) for value in resolution["expected_provider_cash_amounts"]]
                if (sorted(group.dividCashPsBeforeTax.map(Decimal)) != sorted(components)
                        or Decimal(resolution["dividCashPsBeforeTax"]) != sum(components)
                        or any(resolution.get(field) != "0" for field in amounts[1:])):
                    raise ValueError(f"Issuer combined cash components changed: {symbol} {operation_date}")
            else:
                choices = group.loc[group.dividCashPsBeforeTax.map(Decimal).eq(Decimal(resolution["dividCashPsBeforeTax"]))]
            used.add((symbol, operation_date))
        chosen = max(choices.index, key=lambda index: sum(bool(value) for field, value in actions.loc[index].items()
                                                      if field != "_cache_file"))
        keep.append(chosen)
        if combined:
            overrides[chosen] = {field: resolution[field] for field in amounts}
            if "dividCashPsAfterTax" in actions:
                overrides[chosen]["dividCashPsAfterTax"] = ""
        audit.append({"symbol": symbol, "dividOperateDate": operation_date,
                      "status": ("issuer_combined_cash_event" if combined else
                                 "issuer_corrected_cash_event" if conflict else "same_economic_event_collapsed"),
                      "chosen_cache_file": actions.at[chosen, "_cache_file"],
                      "source_rows": group.to_dict("records"),
                      **({"canonical_overrides": overrides[chosen]} if combined else {}),
                      **({"issuer_resolution": resolution} if conflict or combined else {})})
    if set(resolved) != used:
        raise ValueError("Issuer cash-correction resolution no longer matches a conflicting source event")
    canonical = actions.loc[sorted(keep)].copy()
    for index, values in overrides.items():
        for field, value in values.items():
            canonical.at[index, field] = value
    canonical = canonical.drop(columns="_cache_file").reset_index(drop=True)
    return canonical, audit


def supplement_actions(root, actions, symbols):
    """Add evidenced missing cash dividends; never overwrite provider facts."""
    root = Path(root).resolve()
    path = root / "supplements/corporate_actions.csv"
    if not path.exists():
        return actions, []
    supplements = pd.read_csv(path, dtype=str, keep_default_na=False)
    dates = ("dividRegistDate", "dividOperateDate", "dividPayDate")
    amounts = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
    required = {"symbol", *dates, *amounts, "source_url", "source_file", "source_date"}
    if not required <= set(supplements):
        raise ValueError("Cash-dividend supplement schema incomplete")
    supplements = supplements.loc[supplements.symbol.isin(symbols)]
    if supplements.duplicated(["symbol", "dividOperateDate"]).any():
        raise ValueError("Duplicate cash-dividend supplement")
    additions, evidence = [], []
    for row in supplements.to_dict("records"):
        chronology = [date.fromisoformat(row[k]) for k in ("source_date", *dates)]
        if chronology != sorted(chronology) or not chronology[1] < chronology[2]:
            raise ValueError("Cash-dividend supplement has inconsistent dates")
        values = [Decimal(row[k]) for k in amounts]
        if not all(v.is_finite() for v in values) or values[0] <= 0 or any(values[1:]):
            raise ValueError("Only positive cash-only dividend supplements are supported")
        source = (root / row["source_file"]).resolve()
        source.relative_to(root)
        if not row["source_url"].startswith("https://") or not source.is_file():
            raise ValueError("Dividend supplement requires an archived primary-source PDF and URL")
        with source.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Dividend source archive is not a PDF")
        row["source_file"] = source.relative_to(root).as_posix()
        existing = actions.loc[actions.symbol.eq(row["symbol"]) & actions.dividOperateDate.eq(row["dividOperateDate"])]
        if len(existing) > 1:
            raise ValueError("Duplicate provider action before supplementation")
        if len(existing):
            previous = existing.iloc[0]
            if (any(previous[k] != row[k] for k in dates)
                    or any(Decimal(previous.get(k, "") or "0") != Decimal(row[k]) for k in amounts)):
                raise ValueError("Primary-source dividend conflicts with provider; reconcile explicitly")
            status = "matches_provider_not_added"
        else:
            # Missing-event supplements enter the same identity projection as provider rows.
            # Keep the CSV contract small, but derive the canonical provider code from the
            # already validated symbol before the projection step runs.
            row["code"] = normalize_symbol(row["symbol"]).baostock_code
            row["_cache_file"] = row["source_file"]
            additions.append(row)
            status = "added_missing_cash_dividend"
        evidence.append({**row, "status": status})
    combined = pd.concat([actions, pd.DataFrame(additions)], ignore_index=True).fillna("") if additions else actions
    return combined, evidence


def documented_price_adjustments(root, symbols, start, end=None):
    """Pin references and restricted distribution terms, not account eligibility."""
    root = Path(root).resolve()
    path = root / "supplements/price_adjustments.csv"
    required = {"symbol", "date", "kind", "registration_date", "source_date", "source_url", "source_file"}
    rows = pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else pd.DataFrame(columns=sorted(required))
    distribution_path = root / "supplements/restructuring_distributions.json"
    if distribution_path.exists():
        payload = json.loads(distribution_path.read_text(encoding="utf-8"))
        rows = pd.concat([rows, pd.DataFrame(payload["events"])], ignore_index=True).fillna("")
    if not required <= set(rows):
        raise ValueError("Price-adjustment supplement schema incomplete")
    rows = rows.loc[rows.symbol.isin(symbols)]
    if rows.duplicated(["symbol", "date"]).any():
        raise ValueError("Duplicate price-adjustment supplement")
    evidence = []
    for row in rows.to_dict("records"):
        event = date.fromisoformat(row["date"])
        if (row["kind"] not in {"rights_issue", "restructuring_no_entitlement", "restructuring_holder_distribution"}
                or not date.fromisoformat(row["registration_date"]) < event
                or date.fromisoformat(row["source_date"]) > event):
            raise ValueError("Price adjustment kind or chronology is unsupported")
        if event >= date.fromisoformat(start) and end is None:
            raise ValueError("Only documented pre-replay adjustments or explicitly bounded replay references are supported")
        if end is not None and event > date.fromisoformat(end):
            raise ValueError("Price adjustment is outside the snapshot end")
        if row["kind"] == "rights_issue":
            ratio, price = Decimal(row["rights_ratio"]), Decimal(row["subscription_price"])
            if not ratio.is_finite() or not price.is_finite() or not 0 < ratio <= 1 or price <= 0:
                raise ValueError("Invalid rights-issue ratio or price")
            if event >= date.fromisoformat(start):
                row["ledger_policy"] = "stop_unresolved_registered_holding"
        elif row["kind"] == "restructuring_no_entitlement":
            restructuring_ex_price(1, row)
        else:
            holder_restructuring_ex_price(1, row)
            holder_source = (root / row["holder_source_file"]).resolve()
            holder_source.relative_to(root)
            if (not row["holder_source_url"].startswith("https://")
                    or date.fromisoformat(row["holder_source_date"]) > event
                    or not holder_source.is_file() or not holder_source.read_bytes().startswith(b"%PDF-")):
                raise ValueError("Holder distribution requires an archived primary-source PDF and date")
            row["holder_source_file"] = holder_source.relative_to(root).as_posix()
            row["holder_source_sha256"] = sha256(holder_source.read_bytes()).hexdigest()
            holder_distribution_action(row)
            if event >= date.fromisoformat(start):
                row["ledger_policy"] = "reconcile_registered_holder_distribution"
        source = (root / row["source_file"]).resolve()
        source.relative_to(root)
        if not row["source_url"].startswith("https://") or not source.is_file():
            raise ValueError("Price adjustment requires an archived primary-source PDF and URL")
        content = source.read_bytes()
        if not content.startswith(b"%PDF-"):
            raise ValueError("Price adjustment source archive is not a PDF")
        evidence.append({**row, "source_file": source.relative_to(root).as_posix(),
                         "source_sha256": sha256(content).hexdigest()})
    return evidence


def prestart_rights_adjustments(root, symbols, start):
    return documented_price_adjustments(root, symbols, start)


def fixed_membership(root):
    folder = Path(root) / "universes" / "hs300_20231229"
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    members = pd.read_csv(folder / "all_members.csv", dtype=str, keep_default_na=False)
    if metadata["requested_date"] != "2023-12-29" or metadata["source"] != "BaoStock.query_hs300_stocks":
        raise ValueError("Unexpected historical membership source/date")
    if len(members) != 300 or members.code.duplicated().any() or len(members) != metadata["members"]:
        raise ValueError("Incomplete/duplicate historical CSI300 membership")
    if members.updateDate.eq("").any() or pd.to_datetime(members.updateDate).gt(pd.Timestamp(metadata["requested_date"])).any():
        raise ValueError("Membership is not dated before the comparison period")
    expected_symbols = members.code.map(lambda code: normalize_symbol(code).vt_symbol)
    if not expected_symbols.equals(members.symbol):
        raise ValueError("Historical membership code/symbol mismatch")
    main = members.loc[members.code.str.fullmatch(r"(?:sh\.60\d{4}|sz\.00\d{4})")].sort_values("symbol")
    priority = pd.read_csv(folder / "mainboard_priority.csv", dtype=str, keep_default_na=False)
    if (len(main) != metadata["mainboard_members"] or len(priority) != len(main)
            or set(main.symbol) != set(priority.symbol)):
        raise ValueError("Priority list no longer matches historical main-board membership")
    return main, metadata


def fixed_coverage(root=DEFAULT_ROOT):
    root = Path(root)
    members, metadata = fixed_membership(root)
    plan = json.loads((root / "collection_plan.json").read_text(encoding="utf-8"))
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False).set_index("symbol")
    missing, complete = [], []
    for symbol in members.symbol:
        if symbol not in universe.index or universe.loc[symbol, "board"] != "main":
            raise ValueError(f"Historical member missing or unsupported in provider master: {symbol}")
        item = universe.loc[symbol]
        if (not item.ipoDate or item.ipoDate > metadata["requested_date"]
                or (item.outDate and item.outDate <= metadata["requested_date"])):
            raise ValueError(f"Historical membership conflicts with listing interval: {symbol}")
        receipt = load_receipt(root, symbol)
        pending = [key for key, relative, year in tasks_for(plan, symbol)
                   if not task_complete(root, receipt, (key, relative, year))]
        if pending:
            missing.append({"symbol": symbol, "missing_tasks": pending})
        else:
            complete.append(symbol)
    return {"scope": FIXED_SCOPE, "source": metadata["source"],
            "selection_date": metadata["requested_date"], "expected_symbols": len(members),
            "complete_symbols": len(complete), "missing": missing,
            "source_files_complete": not missing, "snapshot_validated": False,
            "membership_independently_verified": metadata.get("exchange_independently_verified", False)}


def validate_pair(raw, adjusted, item, calendar):
    issues = []
    for kind, frame, flag in (("raw", raw, "3"), ("adjusted", adjusted, "2")):
        quality = bar_quality(frame, item, calendar, flag)
        if quality["missing_listing_date"]:
            issues.append(f"{kind}: missing listing date")
        for field in ("invalid_price_dates", "invalid_status_dates", "invalid_activity_dates"):
            if quality[field]:
                issues.append(f"{kind}: {field}: {quality[field]}")
        # outDate is a metadata boundary, not an instruction to invent a final bar.
        missing = [d for d in quality["missing_calendar_dates"] if d != item["outDate"]]
        if missing:
            issues.append(f"{kind}: missing in-listing sessions: {missing}")
    keys = ["date", "symbol"]
    if not raw[keys].sort_values(keys).reset_index(drop=True).equals(
            adjusted[keys].sort_values(keys).reset_index(drop=True)):
        issues.append("raw/adjusted date-symbol mismatch")
    active = raw.tradestatus.eq("1") & pd.to_numeric(raw.volume, errors="coerce").gt(0)
    vwap = pd.to_numeric(raw.amount, errors="coerce") / pd.to_numeric(raw.volume, errors="coerce")
    outside = active & (vwap.lt(pd.to_numeric(raw.low) - .011) | vwap.gt(pd.to_numeric(raw.high) + .011))
    if outside.any():
        issues.append(f"raw amount/volume outside OHLC: {raw.loc[outside, 'date'].tolist()}")
    return issues


def build(root=DEFAULT_ROOT, destination=None, fixed_hs300=False):
    root = Path(root)
    plan = json.loads((root / "collection_plan.json").read_text(encoding="utf-8"))
    universe = pd.read_csv(root / "universe.csv", dtype=str, keep_default_na=False)
    calendar_frame = pd.read_csv(root / "calendar.csv", dtype=str)
    calendar = calendar_frame.date.tolist()
    selection = None
    if fixed_hs300:
        selection = fixed_coverage(root)
        if not selection["source_files_complete"]:
            raise ValueError(f"Fixed universe incomplete: {selection['complete_symbols']}/{selection['expected_symbols']}; use --coverage for missing tasks")
        members, membership_metadata = fixed_membership(root)
        selected = universe.set_index("symbol", drop=False).loc[members.symbol].to_dict("records")
    else:
        selected = []
        for item in universe.loc[universe.board.isin(["main", "chinext", "star"])].to_dict("records"):
            receipt = load_receipt(root, item["symbol"])
            if all(task_complete(root, receipt, task) for task in tasks_for(plan, item["symbol"])):
                selected.append(item)
    if not selected:
        raise ValueError("No fully acquired SH/SZ equity instruments")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    destination = Path(destination) if destination else root / "snapshots" / identifier
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "validating", "folder": str(destination.resolve()), "source": plan["source"],
                "source_root": str(root.resolve()), "created_at": datetime.now().astimezone().isoformat(),
                "scope": INGESTION_SCOPE,
                "selection_date": "2023-12-29", "start": "2024-01-02", "end": plan["last_calendar_session"],
                "symbols": [i["symbol"] for i in selected], "selection_is_point_in_time": False,
                "full_market_symbols_expected": len(universe),
                "mainboard_symbols_expected": int(universe.board.eq("main").sum()),
                "mainboard_symbols_acquired": sum(i["board"] == "main" for i in selected),
                "acquired_board_counts": pd.Series([i["board"] for i in selected]).value_counts().to_dict(),
                "strategy_validated": False, "live_ready": False,
                "limitations": plan["limitations"] + [
                    "Partial acquisition order uses retrieval-time listing status; this is not a representative strategy universe",
                    "selection_date only starts the existing engine after 2023 warmup, not a historical membership claim",
                    "Established SH/SZ ordinary-equity 2024+ daily-auction rules; IPO first five sessions are not simulated",
                    "No new default feed or account pointer; account/client board permissions remain unverified",
                    "Provider outDate itself may have no quote; unresolved held delistings stop replay",
                    "Passing ingestion checks does not independently verify source completeness or corporate actions"]}
    if fixed_hs300:
        manifest.update(scope=FIXED_SCOPE, selection_is_point_in_time=True,
                        selection_basis="provider_reported_preperiod_membership",
                        selection=selection, strategy_validated=False,
                        limitations=plan["limitations"] + [
                            "Fixed pre-period main-board CSI300 subset; not all CSI300, all A shares or a rolling index portfolio",
                            "Historical membership is provider-reported, not independently index-company reconciled",
                            "Every historical member is retained regardless of retrieval-time listing status",
                            "Main-board 2024+ rules only; no new default feed or account pointer",
                            "Unresolved held delistings or missing valuations stop replay; no assumed recovery value"])
    write_json(destination / "manifest.json", manifest)
    raw_frames, adjusted_frames, action_frames, issues = [], [], [], []
    try:
        write_frame(destination / "calendar.csv", calendar_frame)
        write_frame(destination / "instruments.csv", pd.DataFrame(selected))
        if fixed_hs300:
            write_frame(destination / "selection.csv", members)
            write_json(destination / "selection_source.json", membership_metadata)
        for item in selected:
            symbol = item["symbol"]
            raw = pd.read_parquet(root / "raw" / f"{symbol}.parquet")
            adjusted = pd.read_parquet(root / "adjusted" / f"{symbol}.parquet")
            issues += [{"symbol": symbol, "issue": issue} for issue in validate_pair(raw, adjusted, item, calendar)]
            raw_frames.append(raw)
            adjusted_frames.append(adjusted)
            for _, relative, year in tasks_for(plan, symbol, "actions"):
                frame = pd.read_csv(root / relative, dtype=str, keep_default_na=False)
                frame["_cache_file"] = relative
                action_frames.append(frame)
        raw, adjusted, actions = (pd.concat(parts, ignore_index=True) for parts in
                                  (raw_frames, adjusted_frames, action_frames))
        resolutions = load_action_resolutions(root, manifest["symbols"])
        actions, duplicate_audit = reconcile_provider_actions(actions, resolutions)
        write_json(destination / "provider_action_duplicates.json", duplicate_audit)
        if resolutions:
            manifest["provider_action_resolutions"] = resolutions
            write_json(destination / "supplements/provider_action_resolutions.json", resolutions)
            for row in resolutions:
                for prefix in ("", "plan_"):
                    if row.get(prefix + "source_file"):
                        target = destination / row[prefix + "source_file"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(root / row[prefix + "source_file"], target)
        actions, evidence = supplement_actions(root, actions, manifest["symbols"])
        if evidence:
            manifest["corporate_action_supplements"] = evidence
            write_frame(destination / "supplements/corporate_actions.csv", pd.DataFrame(evidence))
            for row in evidence:
                target = destination / row["source_file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / row["source_file"], target)
        adjustments = documented_price_adjustments(root, manifest["symbols"], manifest["start"], manifest["end"])
        if adjustments:
            manifest["historical_price_adjustments"] = adjustments
            for row in adjustments:
                for field in ("source_file", "holder_source_file"):
                    if row.get(field):
                        target = destination / row[field]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(root / row[field], target)
            distribution_actions = [holder_distribution_action(row) for row in adjustments
                                    if row["kind"] == "restructuring_holder_distribution"]
            if distribution_actions:
                actions = pd.concat([actions, pd.DataFrame(distribution_actions)], ignore_index=True).fillna("")
        # Preserve every source action; HistoricalFeed rejects duplicate credit identities.
        write_frame(destination / "raw.parquet", raw)
        write_frame(destination / "adjusted.parquet", adjusted)
        write_frame(destination / "actions.csv", actions)
        write_json(destination / "ingestion_issues.json", issues)
        if issues:
            raise ValueError(f"{len(issues)} unresolved ingestion discrepancies; see ingestion_issues.json")
        manifest.update(status="ready", rows=len(raw), action_rows=len(actions))
        write_json(destination / "manifest.json", manifest)
        HistoricalFeed(destination)
        if selection is not None:
            selection["snapshot_validated"] = True
            write_json(destination / "manifest.json", manifest)
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(destination / "manifest.json", manifest)
        raise
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixed-hs300", action="store_true")
    parser.add_argument("--coverage", action="store_true", help="Read fixed-universe completeness without building a snapshot")
    args = parser.parse_args(argv)
    if args.coverage:
        print(json.dumps(fixed_coverage(args.root), ensure_ascii=False, indent=2))
    else:
        print(build(args.root, args.out, args.fixed_hs300))


if __name__ == "__main__":
    main()
