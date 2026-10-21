"""Batch literal issuer reference formulas into the existing replay consumer."""
import argparse
from bisect import bisect_right
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import uuid

import pandas as pd

from ..market_dataset import write_json
from ..market_snapshot import load_action_resolutions
from ..settings import DATA_ROOT
from .action_disclosures import (DAY, NUMBER, collect, explicit_cash_terms, is_implementation_title,
                                 prepare, validate_primary_notice_source)
from .action_reviewed_amendment import KINDS, corrected_actions
from .adjusted_prices import checked_file


PARSER_ID = "literal_issuer_distribution_reference_v12"
LIMITATION = ("Literal primary implementation reference formulas only; holder entitlements are pinned "
              "provider or exact reviewed issuer terms. No reverse-solving from quotes, PIT-completeness or execution admission.")
FIELDS = ("code", "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate",
          "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
CLOSE = r"(?:\u80a1\u6743\u767b\u8bb0\u65e5|\u9664\u6743\u9664\u606f\u65e5?\u524d(?:\u4e00|1)\u4ea4\u6613\u65e5)\u6536\u76d8\u4ef7(?:\u683c)?"
UNIT = r"(?:\u5143(?:/\u80a1)?|\u80a1)?"
FORMULA = re.compile(r"=(?:\()?" + CLOSE + "-" + NUMBER + UNIT + r"\)/(?:\()1\+" + NUMBER + UNIT + r"\)")
REFERENCE_LABEL = re.compile(r"\u9664\u6743\u9664\u606f(?:\u53c2\u8003\u4ef7|\u4ef7\u683c|\u4ef7)")
CASH_REFERENCE = re.compile(r"(?:\u672c\u6b21A\u80a1)?\u9664\u6743\u9664\u606f\u53c2\u8003\u4ef7\u683c="
                            r"\u524d\u6536\u76d8\u4ef7\u683c-" + NUMBER + r"\u5143(?=[\u3002\uff1b;]|$)")
EVALUATED_CASH = re.compile(r"=" + CLOSE + "-" + NUMBER + r"\u5143/?\u80a1(?=[\u3002\uff1b;]|$)")
PAREN_EVALUATED_CASH = re.compile(r"=\(" + CLOSE + "-" + NUMBER + r"\)\u5143/\u80a1(?=[\u3002\uff1b;]|$)")
PAREN_CASH_LABEL = re.compile(r"\u9664\u6743(?:\u9664\u606f|\(\u606f\))\u53c2\u8003\u4ef7(?:\u683c)?")
EVALUATED_PINNED_CASH = re.compile(r"=" + CLOSE + "-" + NUMBER + r"(?:\u5143(?:/\u80a1)?)?(?=[\u3002\uff1b;]|$)")
SSE_FORMULA = re.compile(r"=\(\u524d\u6536\u76d8\u4ef7\u683c-" + NUMBER + r"\)/\(1\+" + NUMBER + r"\)")
SSE_REFERENCE_LABEL = re.compile(r"\u9664\u6743\(\u606f\)\u53c2\u8003\u4ef7\u683c")


def compact(value):
    return re.sub(r"\s+", "", value).translate(str.maketrans({
        "\uff08": "(", "\uff09": ")", "\uff1d": "=", "\uff0d": "-", "\u2212": "-", "\u00f7": "/", "\uff0b": "+"}))


def sse_reserve_terms(text, action, holder_cash, reserve):
    sections = re.findall(r"\u4e8c\u3001\u5206\u914d\u3001\u8f6c\u589e\u80a1\u672c\u65b9\u6848(.*?)\u4e09\u3001", text)
    if len(sections) != 1 or not action["symbol"].endswith(".SSE"):
        raise ValueError("Unique SSE current distribution section required")
    section = sections[0]
    cash = {Decimal(v) for v in re.findall(
        r"\u6bcf\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229" + NUMBER + r"\u5143\(\u542b\u7a0e\)", section)}
    shares = {Decimal(v) for v in re.findall(r"\u4ee5\u8d44\u672c\u516c\u79ef\u6bcf\u80a1\u8f6c\u589e" + NUMBER + r"\u80a1", section)}
    # Some SSE implementation notices state the same reserve transfer as
    # "each share transfer" in the top-level ratio table.
    direct_shares = {Decimal(v) for v in re.findall(r"\u6bcf\u80a1\u8f6c\u589e\u80a1\u4efd" + NUMBER + r"\u80a1", text)}
    shares |= direct_shares
    if any(Decimal(v) > 0 for v in re.findall(
            r"(?:\u6bcf\u80a1|\u6bcf10\u80a1)?(?:\u9001\u7ea2\u80a1|\u9001\u80a1)" + NUMBER + r"\u80a1", text)):
        raise ValueError("Issuer bonus-stock terms conflict with pinned zero bonus")
    cash_ten = {Decimal(v) / 10 for v in re.findall(
        r"\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229" + NUMBER + r"\u5143\(\u542b\u7a0e\)", section)}
    shares_ten = {Decimal(v) / 10 for v in re.findall(
        r"\u4ee5\u8d44\u672c\u516c\u79ef\u91d1\u5411\u5168\u4f53\u80a1\u4e1c\u6bcf10\u80a1\u8f6c\u589e" + NUMBER + r"\u80a1", section)}
    if cash_ten or shares_ten:
        # The caller already requires a known zero bonus. This template never completes it.
        if (cash_ten != {holder_cash} or shares_ten != {reserve}
                or (cash and cash != {holder_cash}) or (shares and shares != {reserve})
                or any(Decimal(v) > 0 for v in re.findall(
                    r"(?:\u6bcf10\u80a1)?(?:\u9001\u7ea2\u80a1|\u9001\u80a1)" + NUMBER + r"\u80a1", section))):
            raise ValueError("Explicit SSE per-ten cash/reserve must equal known holder terms")
    else:
        if (cash != {holder_cash} or shares != {reserve}
                or ("\u4e0d\u9001\u7ea2\u80a1" not in section and not direct_shares)):
            raise ValueError("Explicit SSE cash/reserve/zero-bonus must equal pinned holder terms")
    slash_day = r"([0-9]{4})/([0-9]{1,2})/([0-9]{1,2})"
    header = (r"\u80a1\u6743\u767b\u8bb0\u65e5\u9664\u6743\(\u606f\)\u65e5"
              r"\u65b0\u589e\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u4efd\u4e0a\u5e02\u65e5"
              r"\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5")
    tables = re.findall(header + slash_day * 4, text)
    ashare_header = (r"\u80a1\u4efd\u7c7b\u522b\u80a1\u6743\u767b\u8bb0\u65e5\u6700\u540e\u4ea4\u6613\u65e5"
                    r"\u9664\u6743\(\u606f\)\u65e5"
                    r"\u65b0\u589e\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u4efd\u4e0a\u5e02\u65e5"
                    r"\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5")
    tables += re.findall(ashare_header + r"(?:A|\uff21)\u80a1" + slash_day + "-" + slash_day * 3, text)
    expected = [action[field] for field in (
        "dividRegistDate", "dividOperateDate", "dividStockMarketDate", "dividPayDate")]
    if not tables or any([date(*map(int, table[i:i + 3])).isoformat() for i in range(0, 12, 3)] != expected
                         for table in tables):
        raise ValueError("SSE literal four-column record/ex/list/payment table must match pinned dates")


def pinned_szse_cash_reference_terms(text, action, holder_cash):
    # This reference-only template must not fill blank or alter known share entitlements.
    if (not action["symbol"].endswith(".SZSE")
            or Decimal(action["dividStocksPs"]) != 0 or Decimal(action["dividReserveToStockPs"]) != 0
            or "A\u80a1\u5229\u6da6\u5206\u914d\u5b9e\u65bd\u516c\u544a" not in text
            or "\u4e0d\u4ee5\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c" not in text):
        raise ValueError("Known-zero SZSE cash reference template required; no holder completion")
    cash = {Decimal(v) / 10 for v in re.findall(r"\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1" + NUMBER + r"\u5143", text)}
    if cash != {holder_cash}:
        raise ValueError("Pinned cash reference needs matching explicit per-ten holder cash")
    if any(Decimal(value) > 0 for value in re.findall(
            r"(?:\u6bcf(?:10)?\u80a1)?(?:\u9001\u7ea2\u80a1|\u9001\u80a1|\u8f6c\u589e)" + NUMBER + r"\u80a1", text)):
        raise ValueError("Issuer nonzero share statement conflicts with pinned zero-share reference")
    for field, label in (("dividRegistDate", r"\u80a1\u6743\u767b\u8bb0\u65e5\u4e3a"),
                         ("dividOperateDate", r"\u9664\u606f\u65e5(?:\u53ca\u73b0\u91d1\u7ea2\u5229\u53d1\u653e\u65e5)?\u4e3a"),
                         ("dividPayDate", r"\u73b0\u91d1\u7ea2\u5229\u5c06\u4e8e")):
        found = {date(*map(int, match)).isoformat() for match in re.findall(label + r"[:\uff1a]?" + DAY, text)}
        if found != {action[field]}:
            raise ValueError("Pinned cash reference literal record/ex/payment dates conflict")
    if action["dividStockMarketDate"]:
        raise ValueError("Known zero-share reference cannot claim a new-share listing")


def extract(pages, action, source_date):
    if not pages or any(not isinstance(p, str) or not p.strip() for p in pages):
        raise ValueError("Complete text-bearing implementation PDF required")
    text, first = compact("".join(pages)), compact(pages[0])
    code = action["symbol"].split(".")[0]
    heading = re.split(r"\u91cd\u8981\u5185\u5bb9\u63d0\u793a|\u7279\u522b\u63d0\u793a", first, maxsplit=1)[0]
    if not re.search(r"\u8bc1\u5238\u4ee3\u7801[:\uff1a]?" + code + r"(?![0-9])", first) or not is_implementation_title(heading):
        raise ValueError("Issuer code and implementation title required on the first page")
    footer_days = re.findall(DAY, compact(pages[-1]))
    if not footer_days:
        footer_days = re.findall(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$", compact(pages[-1]))
    if not footer_days:
        raise ValueError("Dated issuer footer required")
    footer_day = date(*map(int, footer_days[-1])).isoformat()
    if not footer_day <= source_date <= action["dividRegistDate"] < action["dividOperateDate"]:
        raise ValueError("Issuer footer/publication/record/ex chronology conflict")
    if any(action[field] == "" for field in FIELDS[5:]):
        raise ValueError("Reference extraction cannot complete blank holder entitlements")
    holder_cash, bonus, reserve = (Decimal(action[f]) for f in FIELDS[5:])
    if any(not n.is_finite() or n < 0 for n in (holder_cash, bonus, reserve)) or bonus != 0:
        raise ValueError("Only known nonnegative cash/reserve and pinned zero-bonus template supported")
    sse_template = reserve > 0 and "\u4e8c\u3001\u5206\u914d\u3001\u8f6c\u589e\u80a1\u672c\u65b9\u6848" in text
    pinned_cash_template = reserve == 0 and "A\u80a1\u5229\u6da6\u5206\u914d\u5b9e\u65bd\u516c\u544a" in first
    if reserve == 0:
        if pinned_cash_template:
            pinned_szse_cash_reference_terms(text, action, holder_cash)
        else:
            explicit_cash_terms(pages, action)
    elif sse_template:
        sse_reserve_terms(text, action, holder_cash, reserve)
    else:
        sections = re.findall(r"\u4e8c[\u3001.]\u672c\u6b21\u5b9e\u65bd\u7684(?:\u6743\u76ca\u5206\u6d3e|"
                              r"\u5229\u6da6\u5206\u914d(?:\u548c|\u53ca)\u8d44\u672c\u516c\u79ef\u91d1\u8f6c\u589e\u80a1\u672c)\u65b9\u6848(.*?)"
                              r"\u4e09[\u3001.]", text)
        if len(sections) != 1:
            raise ValueError("Unique current implementation section required")
        section = sections[0]
        cash = {Decimal(v) / 10 for v in re.findall(
            r"\u6bcf10\u80a1\u6d3e" + NUMBER + r"\u5143\u4eba\u6c11\u5e01\u73b0\u91d1\(\u542b\u7a0e", section)}
        cash |= {Decimal(v) / 10 for v in re.findall(
            r"\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229" + NUMBER + r"\u5143\(\u542b\u7a0e", section)}
        shares = {Decimal(v) / 10 for v in re.findall(r"\u6bcf10\u80a1\u8f6c\u589e" + NUMBER + r"\u80a1", section)}
        if cash != {holder_cash} or shares != {reserve} or not re.search(r"\u4ee5\u8d44\u672c\u516c\u79ef\u91d1", section):
            raise ValueError("Explicit holder cash and reserve must equal the pinned event")
        if re.search(r"(?:\u9001\u7ea2\u80a1|\u6bcf10\u80a1\u9001)" + NUMBER, section):
            raise ValueError("Bonus-containing implementation needs a separate supported template")
        for field, label in (("dividRegistDate", r"\u80a1\u6743\u767b\u8bb0\u65e5"),
                             ("dividOperateDate", r"\u9664\u6743\u9664\u606f\u65e5")):
            found = {date(*map(int, m)).isoformat() for m in re.findall(label + r"(?:\u4e3a)?[:\uff1a]?" + DAY, text)}
            if found != {action[field]}:
                raise ValueError("Unique literal record/ex dates must match the pinned event")
        paid = {date(*map(int, m)).isoformat() for m in re.findall(
            r"\u73b0\u91d1\u7ea2\u5229(?:\u5c06)?\u4e8e" + DAY + r"[^\u3002]{0,120}\u8d44\u91d1\u8d26\u6237", text)}
        listed = {date(*map(int, m)).isoformat() for m in re.findall(
            r"\u65e0\u9650\u552e\u6761\u4ef6\u6d41\u901a\u80a1\u7684\u8d77\u59cb\u4ea4\u6613\u65e5(?:\u4e3a)?[:\uff1a]?" + DAY, text)}
        if paid != {action["dividPayDate"]} or listed != {action["dividStockMarketDate"]}:
            raise ValueError("Explicit payment/listing dates must match the pinned event")
    formulas, source_pages = set(), []
    for page_no, page in enumerate(pages, 1):
        page_text = compact(page)
        for match in FORMULA.finditer(page_text):
            if not REFERENCE_LABEL.search(page_text[max(0, match.start() - 350):match.start()]):
                continue
            formulas.add(tuple(Decimal(v) for v in match.groups()))
            source_pages.append(page_no)
        if action["symbol"].endswith(".SSE"):
            for match in SSE_FORMULA.finditer(page_text):
                if not SSE_REFERENCE_LABEL.search(page_text[max(0, match.start() - 350):match.start()]):
                    continue
                formulas.add(tuple(Decimal(v) for v in match.groups()))
                source_pages.append(page_no)
        # A cash-only literal omits the divisor; its zero-share terms were explicitly checked above.
        if reserve == 0:
            for match in CASH_REFERENCE.finditer(page_text):
                formulas.add((Decimal(match.group(1)), Decimal(0)))
                source_pages.append(page_no)
            if pinned_cash_template:
                for match in EVALUATED_PINNED_CASH.finditer(page_text):
                    if not REFERENCE_LABEL.search(page_text[max(0, match.start() - 350):match.start()]):
                        continue
                    formulas.add((Decimal(match.group(1)), Decimal(0)))
                    source_pages.append(page_no)
            for match in EVALUATED_CASH.finditer(page_text):
                if not REFERENCE_LABEL.search(page_text[max(0, match.start() - 350):match.start()]):
                    continue
                formulas.add((Decimal(match.group(1)), Decimal(0)))
                source_pages.append(page_no)
            for match in PAREN_EVALUATED_CASH.finditer(page_text):
                if not PAREN_CASH_LABEL.search(page_text[max(0, match.start() - 350):match.start()]):
                    continue
                formulas.add((Decimal(match.group(1)), Decimal(0)))
                source_pages.append(page_no)
    if len(formulas) != 1:
        raise ValueError("Unique explicitly evaluated cash/share reference formula required; no inferred ratio")
    cash, shares = next(iter(formulas))
    if not 0 <= cash <= holder_cash or not 0 <= shares <= reserve or (reserve > 0 and shares == 0):
        raise ValueError("Literal treasury-exclusion formula conflicts with holder cash/share terms")
    return {"symbol": action["symbol"], **{field: action[field] for field in FIELDS},
            "reference_adjustment_cash_ps": str(cash), "reference_adjustment_share_ratio": str(shares),
            "source_pages": sorted(set(source_pages)), "text_pages_read": list(range(1, len(pages) + 1)),
            "issuer_confirmed_reference": True, "holder_bonus_basis": "unchanged pinned provider zero, not filled from absence",
            "holder_terms_basis": ("Known provider cash/zero-share fields unchanged; issuer confirms cash/dates/reference and no reserve, not a new bonus confirmation"
                                   if pinned_cash_template else "Pinned provider or exact reviewed issuer entitlements, unchanged"),
            "basis": LIMITATION, "parser_id": PARSER_ID, "execution_admitted": False}


def priorities(run_folder):
    run_folder = Path(run_folder).resolve()
    record = json.loads((run_folder / "run.json").read_text(encoding="utf-8"))
    if record.get("research_family") != "weekly_reversal" or record.get("status") != "failed_weekly_reversal_diagnostic":
        raise ValueError("Actual failed fixed weekly replay required")
    artifacts = Path(record["artifacts"])
    ranks = pd.read_csv(artifacts / "source_target_ranks.csv", dtype={"date": str, "symbol": str})
    states = [json.loads((artifacts / signal / "account.json").read_text(encoding="utf-8"))
              for signal in record["protocol"]["signals"]]
    frontier = min(s["day"] for s in states)
    held = {symbol for state in states for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    days = sorted(ranks.date.unique())
    groups = ranks.groupby("date").symbol.apply(set).to_dict()
    refs = pd.read_parquet(Path(record["inputs"]["reference_view"]) / "raw_reference_changes.parquet")
    rows = refs.loc[refs.reference_formula_status.eq("reference_mismatch") & refs.date.gt(frontier)].sort_values(["date", "symbol"])
    selected = []
    for row in rows.to_dict("records"):
        i = bisect_right(days, row["dividRegistDate"])
        if row["symbol"] in held or any(row["symbol"] in groups[d] for d in days[max(0, i - 2):i]):
            selected.append((row["symbol"], row["date"]))
    return record, {"source_run": str(run_folder), "events": selected, "frontier": frontier,
                    "basis": "Data acquisition only: prior two unchanged rank30 decisions at registration or positive saved holdings; not future quality universe filtering or exhaustive future execution exposure",
                    "execution_admitted": False}


def publish(disclosures_root, selection=None, *, reviewed_root=None):
    source = Path(disclosures_root).resolve()
    plan = json.loads((source / "plan.json").read_text(encoding="utf-8"))
    state = json.loads((source / "result.json").read_text(encoding="utf-8"))
    direct = state["status"] == "direct_notice_batch_completed"
    if (state["status"] not in {"batch_completed", "direct_notice_batch_completed"}
            or plan.get("execution_admitted") is not False
            or (direct and (state.get("execution_admitted") is not False
                           or state.get("unattempted_events") != 0
                           or sorted((r["symbol"], r["date"]) for r in state["records"])
                           != sorted(tuple(key) for key in plan["explicit_priority_events"])))):
        raise ValueError("Completed unadmitted primary disclosure batch required")
    actions = pd.read_parquet(checked_file(source, "input_actions.parquet", plan["input_actions_sha256"]))
    corrected, reviews, audit = actions, [], []
    if reviewed_root is not None:
        reviewed_root = Path(reviewed_root).resolve()
        reviews = load_action_resolutions(reviewed_root, set(actions.symbol))
        keys = {(entry["symbol"], entry["date"]) for entry in state["records"]}
        if (not reviews or any(row["kind"] not in KINDS or
                              (row["symbol"], row["dividOperateDate"]) not in keys for row in reviews)):
            raise ValueError("Reviews must exactly bind recorded cash-only disclosure events")
        corrected, audit = corrected_actions(actions, reviews, source / "input_actions.parquet")
    root = DATA_ROOT / "research/stock_distribution_references" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    shutil.copytree(source, root)
    shutil.copy2(Path(__file__), root / "reference_parser.py")
    if reviews:
        stored = []
        for row in reviews:
            name = f"holder_reviews/{row['symbol']}_{row['dividOperateDate']}.pdf"
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked_file(reviewed_root, row["source_file"], row["source_sha256"]), target)
            stored.append({**row, "source_file": name})
        write_json(root / "holder_reviews/resolutions.json", {"resolutions": stored, "audit": audit})
    accepted, records = [], []
    for entry in state["records"]:
        row = {"symbol": entry["symbol"], "date": entry["date"]}
        try:
            action = actions.loc[actions.symbol.eq(entry["symbol"]) & actions.dividOperateDate.eq(entry["date"])]
            if len(action) != 1:
                raise ValueError("Unique pinned action required")
            action = action.iloc[0].to_dict()
            if any(entry["expected_provider_terms"][f] != action[f] for f in FIELDS if f in entry["expected_provider_terms"]):
                raise ValueError("Archived expected holder terms changed")
            action = corrected.loc[corrected.symbol.eq(entry["symbol"]) &
                                   corrected.dividOperateDate.eq(entry["date"])].iloc[0].to_dict()
            validate_primary_notice_source(entry, root)
            checked_file(root, entry["source_file"], entry["source_sha256"])
            pages = json.loads(checked_file(root, entry["text_file"], entry["text_sha256"]).read_text(encoding="utf-8"))["pages"]
            terms = extract(pages, action, entry["source_date"])
            if any(row["symbol"] == entry["symbol"] and row["dividOperateDate"] == entry["date"] for row in reviews):
                terms["holder_review_file"] = "holder_reviews/resolutions.json"
            terms.update({f: entry[f] for f in ("source_date", "source_url", "source_file", "source_sha256")})
            if entry.get("source_channel") == "issuer_linked_archive":
                terms.update({key: entry[key] for key in (
                    "source_channel", "issuer_page_url", "issuer_page_file", "issuer_page_sha256",
                    "issuer_archive_upload_date", "point_in_time_availability_verified")})
            accepted.append(terms)
            row.update(status="literal_reference_extracted", reference_cash=terms["reference_adjustment_cash_ps"],
                       reference_shares=terms["reference_adjustment_share_ratio"])
        except (ValueError, KeyError) as error:
            row.update(status="unresolved_reference", reason=str(error))
        records.append(row)
    write_json(root / "supplements/distribution_references.json", accepted)
    result = {"status": "completed_literal_reference_diagnostic", "disclosures_root": str(source),
              "holder_review_root": str(reviewed_root) if reviewed_root is not None else None,
              "selection": selection, "parser_id": PARSER_ID, "accepted": len(accepted), "unresolved": len(records) - len(accepted),
              "records": records, "limitations": [LIMITATION], "execution_admitted": False}
    write_json(root / "reference_result.json", result)
    return root, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=Path)
    source.add_argument("--disclosures-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pdf-python", type=Path)
    parser.add_argument("--max-events", type=int, default=20)
    parser.add_argument("--reviewed-root", type=Path, help="Exact reviewed cash-only corrections bound to archived source terms")
    args = parser.parse_args(argv)
    selection = None
    if args.run:
        record, selection = priorities(args.run)
        if not args.execute:
            print(json.dumps(selection))
            return 0
        if not args.pdf_python or not 1 <= args.max_events <= 64:
            parser.error("Execution requires the existing PDF interpreter and1..64 max events")
        keys = selection["events"][:args.max_events]
        if not keys:
            print(json.dumps(selection))
            return 0
        root = prepare(record["inputs"]["event_view"], priority_events=keys)
        collect(root, args.pdf_python, max_events=len(keys), priority_events=keys)
    else:
        root = args.disclosures_root
    root, result = publish(root, selection, reviewed_root=args.reviewed_root)
    print(json.dumps({"folder": str(root), **result}))
    return 0 if result["accepted"] else 1
