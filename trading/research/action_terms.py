"""Opt-in secondary reported terms for diagnostic replay, never issuer admission."""
import argparse
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import uuid

from bs4 import BeautifulSoup
import pandas as pd
import requests

from ..market_dataset import normalize_symbol, write_json
from ..market_snapshot import RESERVE_CORRECTION_FIELDS
from ..settings import DATA_ROOT
from .action_disclosures import fetch
from .adjusted_prices import checked_file


CNINFO = "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1139"
SINA = "https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{code}.phtml"
SINA_HEADERS = {"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0", "Connection": "close"}
HEADERS = ["公告日期", "分红方案(每10股)", "送股(股)", "转增(股)",
           "派息(税前)(元)", "进度", "除权除息日", "股权登记日", "红股上市日", "查看详细"]
LIMITATION = ("Opt-in secondary Sina reported zero reserve terms, matched to BaoStock cash/bonus "
              "and CNinfo cash/record/ex/payment dates; not issuer-confirmed share terms, "
              "independent event coverage, PIT availability or execution admission")


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Canonical explicit source date required")
    return value


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(value)):
        raise ValueError("Explicit finite nonnegative source number required; null/dash is not zero")
    return Decimal(str(value))


def sina_rows(raw, code):
    soup = BeautifulSoup(raw.decode("gb18030"), "html.parser")
    title = soup.title.get_text() if soup.title else ""
    if not re.search(r"\(" + code + r"\)分红配股", title):
        raise ValueError("Sina issuer title differs from the requested security")
    tables = soup.select("table#sharebonus_1")
    if len(tables) != 1:
        raise ValueError("Unique source dividend table required")
    table = tables[0]
    labels = [node.get_text(strip=True) for node in table.select("thead strong")]
    if labels != [HEADERS[i] for i in (0, 1, 5, 6, 7, 8, 9, 2, 3, 4)]:
        raise ValueError("Source per10-share/gross-cash table headers changed")
    records = []
    for row in table.select("tbody tr"):
        cells = [node.get_text(strip=True) for node in row.find_all("td", recursive=False)]
        if len(cells) == 1 and cells[0] == "暂时没有数据！":
            continue
        if len(cells) != 9:
            raise ValueError("Source dividend row shape changed")
        records.append(dict(zip(("notice_date", "bonus_per10", "reserve_per10", "cash_per10",
                                 "progress", "ex_date", "record_date", "stock_date", "detail"), cells)))
    return records


def cninfo_rows(raw):
    page = json.loads(raw)
    records = page.get("records")
    if (page.get("resultcode") != 200 or not isinstance(records, list)
            or page.get("count") != len(records) or page.get("total") != len(records)):
        raise ValueError("Complete successful primary history required; pagination is not an empty result")
    return records


def match_terms(action, secondary, primary):
    """Complete only a blank reserve from a literal secondary zero, not a null."""
    def close_amount(left, right):
        # Public dividend tables sometimes retain six-decimal per-10-share values
        # while the canonical provider stores the corresponding per-share value
        # at fewer decimals.  Accept only bounded rounding noise; never coerce a
        # missing or materially different term to zero.
        return abs(left - right) <= Decimal("0.000001")

    if action["dividReserveToStockPs"] != "":
        raise ValueError("Only blank reserve terms may be completed")
    stock_per_share = amount(action["dividStocksPs"])
    if stock_per_share < 0:
        raise ValueError("Provider stock distribution cannot be negative")
    cash = amount(action["dividCashPsBeforeTax"])
    record, ex, payment = (day(action[field]) for field in ("dividRegistDate", "dividOperateDate", "dividPayDate"))
    stock_market_date = action["dividStockMarketDate"]
    if (cash <= 0 or (stock_per_share == 0 and stock_market_date != "")
            or (stock_per_share > 0 and stock_market_date not in {"", ex})):
        raise ValueError("Existing cash/share action dates conflict with the supported distribution")
    if not record < ex <= payment:
        raise ValueError("Existing record/ex/payment chronology differs")
    secondary = [row for row in secondary if row["ex_date"] == ex]
    primary = [row for row in primary if row.get("F020D") == ex]
    if len(secondary) != 1 or len(primary) != 1:
        raise ValueError("Unique matching implemented event required in both histories")
    secondary, primary = secondary[0], primary[0]
    secondary_stock_per_share = amount(secondary["bonus_per10"]) / 10
    if (secondary["progress"] != "实施" or day(secondary["record_date"]) != record
            or not day(secondary["notice_date"]) <= record or secondary["stock_date"] not in {"", "--"}
            or not close_amount(secondary_stock_per_share, stock_per_share)
            or amount(secondary["reserve_per10"]) != 0
            or not close_amount(amount(secondary["cash_per10"]) / 10, cash)):
        raise ValueError("Explicit secondary cash/zero-share/date terms conflict; no unknown-to-zero conversion")
    if (day(primary["F018D"]) != record or day(primary["F023D"]) != payment
            or not day(primary["F006D"]) <= record
            or not close_amount(amount(primary["F012N"]) / 10, cash)):
        raise ValueError("Primary implemented cash/record/payment/date terms conflict")
    primary_stock = primary.get("F010N")
    if stock_per_share == 0:
        if ((primary_stock is not None and amount(primary_stock) != 0)
                or (primary.get("F011N") is not None and amount(primary["F011N"]) != 0)
                or primary.get("F025D") not in {None, ""}):
            raise ValueError("Primary reported share amount conflicts with the secondary zero")
        basis = "secondary_reported_zero_reserve_primary_cash_dates_matched"
    else:
        if (primary_stock is None or not close_amount(amount(primary_stock) / 10, stock_per_share)
                or (primary.get("F011N") is not None and amount(primary["F011N"]) != 0)
                or primary.get("F025D") not in {None, ex}):
            raise ValueError("Primary reported stock terms conflict with the provider distribution")
        basis = "secondary_reported_zero_reserve_primary_cash_stock_dates_matched"
    return {"symbol": action["symbol"], "date": ex,
            "expected_provider_terms": {key: action[key] for key in RESERVE_CORRECTION_FIELDS},
            "confirmed_fields": ["dividReserveToStockPs"], "reported_reserve_per_share": "0",
            "source_basis": basis,
            "primary_share_terms_confirmed": stock_per_share > 0 and primary_stock is not None,
            "secondary_row": secondary, "primary_row": primary,
            "execution_admitted": False, "issuer_confirmed": False}


def candidates(actions, secondary, primary):
    accepted, pending = [], []
    for action in actions:
        try:
            accepted.append(match_terms(action, secondary, primary))
        except (ValueError, KeyError) as error:
            pending.append({"symbol": action["symbol"], "date": action["dividOperateDate"], "reason": str(error)})
    return accepted, pending


def collect(event_view, symbols, destination=None, *, reuse_root=None, retain_archived=True):
    event_view = Path(event_view).resolve()
    result = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "research_action_events_projected" or result["execution_admitted"]:
        raise ValueError("Pinned unadmitted research action view required")
    if result.get("secondary_reported_reserve_completions", 0):
        raise ValueError("Secondary collection requires a primary-only event view; reuse raw histories, not completed zeros")
    panel = checked_file(event_view, "canonical_actions.parquet", result["files"]["canonical_actions.parquet"])
    actions = pd.read_parquet(panel).fillna("")
    archived = {}
    if reuse_root is not None:
        reuse_root = Path(reuse_root).resolve()
        reused = json.loads((reuse_root / "result.json").read_text(encoding="utf-8"))
        if reused["status"] != "completed_secondary_action_terms" or reused["execution_admitted"]:
            raise ValueError("Reuse requires a terminal unadmitted secondary source batch")
        archived = {row["symbol"]: row for row in reused["records"] if row["status"] == "source_histories_compared"}
    symbols = list(symbols)
    if (not symbols or len(set(symbols)) != len(symbols)
            or not set(symbols) <= set(actions.symbol)
            or any(normalize_symbol(symbol).vt_symbol != symbol for symbol in symbols)):
        raise ValueError("Select unique canonical source symbols, not a strategy universe cap")
    symbols = sorted(set(symbols) | set(archived)) if retain_archived else sorted(set(symbols))
    if not set(symbols) <= set(actions.symbol) or len(set(symbols) - set(archived)) > 64:
        raise ValueError("At most64 fresh source queries per batch; reused symbols have no budget cap")
    folder = Path(destination) if destination else DATA_ROOT / "research/secondary_action_terms" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(panel, folder / "input_actions.parquet")
    shutil.copy2(event_view / "result.json", folder / "input_events.json")
    shutil.copy2(Path(__file__), folder / "implementation.py")
    target = actions.loc[actions.dividReserveToStockPs.eq("")]
    output = {"status": "running_secondary_action_terms", "event_view": str(event_view),
              "source_events": len(actions), "unresolved_source_events": len(target),
              "selected_symbols": symbols, "records": [], "execution_admitted": False,
              "automatic_account_promotion": False, "limitation": LIMITATION,
              "reused_root": str(reuse_root) if reuse_root is not None else None,
              "retain_all_archived_histories": retain_archived}
    write_json(folder / "result.json", output)
    from akshare.stock.stock_dividend_cninfo import _get_file_content_ths
    from py_mini_racer import MiniRacer
    js = MiniRacer()
    js.eval(_get_file_content_ths())
    with requests.Session() as session:
        for symbol in symbols:
            code = symbol.split(".")[0]
            dest = folder / symbol
            dest.mkdir()
            row = {"symbol": symbol, "status": "source_error",
                   "observed_at": datetime.now().astimezone().isoformat(),
                   "secondary_url": SINA.format(code=code), "primary_url": CNINFO,
                   "primary_params": {"scode": code}}
            try:
                old = archived.get(symbol)
                if old is not None:
                    if (old["secondary_url"] != row["secondary_url"] or old["primary_url"] != row["primary_url"]
                            or old["primary_params"] != row["primary_params"]):
                        raise ValueError("Archived public source query identity differs")
                    raw = checked_file(reuse_root, old["secondary_file"], old["secondary_sha256"]).read_bytes()
                    row.update(observed_at=old["observed_at"], reused_from=str(reuse_root))
                    if "secondary_request_headers" in old:
                        row["secondary_request_headers"] = old["secondary_request_headers"]
                else:
                    # Avoid truncated compressed transfers observed on large issuer histories.
                    row["secondary_request_headers"] = dict(SINA_HEADERS)
                    raw = fetch(session, "GET", row["secondary_url"], headers=SINA_HEADERS)
                (dest / "secondary.html").write_bytes(raw)
                row.update(secondary_file=f"{symbol}/secondary.html", secondary_sha256=sha256(raw).hexdigest())
                secondary = sina_rows(raw, code)
                if old is not None:
                    raw = checked_file(reuse_root, old["primary_file"], old["primary_sha256"]).read_bytes()
                else:
                    raw = fetch(session, "POST", CNINFO, params=row["primary_params"], headers={
                        "Accept-Enckey": js.call("getResCode1"), "Referer": "http://webapi.cninfo.com.cn/",
                        "User-Agent": "Mozilla/5.0"})
                (dest / "primary.json").write_bytes(raw)
                row.update(primary_file=f"{symbol}/primary.json", primary_sha256=sha256(raw).hexdigest())
                primary = cninfo_rows(raw)
                matched, pending = candidates(target.loc[target.symbol.eq(symbol)].to_dict("records"), secondary, primary)
                row.update(status="source_histories_compared", candidates=matched, pending=pending,
                           secondary_history_rows=len(secondary), primary_history_rows=len(primary))
            except (requests.RequestException, ValueError, KeyError, UnicodeError) as error:
                row["reason"] = f"{type(error).__name__}: {error}"
            output["records"].append(row)
            write_json(folder / "result.json", output)
            print(f"{symbol} {row['status']} candidates={len(row.get('candidates', []))}", flush=True)
    output.update(status="completed_secondary_action_terms", completed_at=datetime.now().astimezone().isoformat(),
                  candidate_events=sum(len(row.get("candidates", [])) for row in output["records"]),
                  pending_selected_events=sum(len(row.get("pending", [])) for row in output["records"]),
                  reused_symbols=sum("reused_from" in row for row in output["records"]),
                  source_errors=sum(row["status"] == "source_error" for row in output["records"]))
    write_json(folder / "result.json", output)
    return folder, output


def apply_reported_terms(actions, root):
    root = Path(root).resolve()
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "completed_secondary_action_terms" or result["execution_admitted"]:
        raise ValueError("Terminal research-only secondary terms required")
    result_actions = actions.copy(deep=True)
    receipts = []
    for row in result["records"]:
        if row["status"] != "source_histories_compared":
            continue
        code = row["symbol"].split(".")[0]
        if (row["secondary_url"] != SINA.format(code=code) or row["primary_url"] != CNINFO
                or row["primary_params"] != {"scode": code}):
            raise ValueError("Secondary/primary query identity changed")
        secondary = sina_rows(checked_file(root, row["secondary_file"], row["secondary_sha256"]).read_bytes(), code)
        primary = cninfo_rows(checked_file(root, row["primary_file"], row["primary_sha256"]).read_bytes())
        for candidate in row["candidates"]:
            key = result_actions.symbol.eq(candidate["symbol"]) & result_actions.dividOperateDate.eq(candidate["date"])
            if int(key.sum()) != 1 or candidate["symbol"] != row["symbol"]:
                raise ValueError("Unique canonical secondary completion identity required")
            action = result_actions.loc[key].iloc[0].to_dict()
            if candidate != match_terms(action, secondary, primary):
                raise ValueError("Secondary candidate no longer matches exact provider/source terms")
            result_actions.loc[key, "dividReserveToStockPs"] = candidate["reported_reserve_per_share"]
            result_actions.loc[key, "reserve_term_basis"] = candidate["source_basis"]
            receipts.append({**candidate, "secondary_url": row["secondary_url"],
                             "primary_url": row["primary_url"], "observed_at": row["observed_at"]})
    return result_actions.fillna(""), receipts


def freeze_prefix_terms(source_root, prefix_root, before, destination=None):
    """Publish new-stage completions without retrospectively changing a pinned prefix."""
    if date.fromisoformat(before).isoformat() != before:
        raise ValueError("Canonical prefix boundary date required")
    source_root, prefix_root = Path(source_root).resolve(), Path(prefix_root).resolve()
    source = json.loads((source_root / "result.json").read_text(encoding="utf-8"))
    prefix = json.loads((prefix_root / "result.json").read_text(encoding="utf-8"))
    if (any(r["status"] != "completed_secondary_action_terms" or r["execution_admitted"] for r in (source, prefix))
            or source["event_view"] != prefix["event_view"]):
        raise ValueError("Prefix freeze requires terminal sources on the same primary-only event view")
    def candidates_by_key(result):
        items = {}
        for row in result["records"]:
            for candidate in row.get("candidates", []):
                key = (candidate["symbol"], candidate["date"])
                if (key in items or key[0] != row["symbol"]
                        or date.fromisoformat(key[1]).isoformat() != key[1]):
                    raise ValueError("Unique canonical candidate identities required")
                items[key] = candidate
        return items
    old, new = candidates_by_key(prefix), candidates_by_key(source)
    retained = {key: value for key, value in old.items() if key[1] < before}
    if any(new.get(key) != value for key, value in retained.items()):
        raise ValueError("Prior completed terms changed or disappeared; cannot preserve this prefix")
    deferred = sorted(key for key in new if key[1] < before and key not in retained)
    output = deepcopy(source)
    for row in output["records"]:
        row["candidates"] = [c for c in row.get("candidates", [])
                             if c["date"] >= before or (c["symbol"], c["date"]) in retained]
    output.update(candidate_events=sum(len(r["candidates"]) for r in output["records"]),
                  prefix_freeze={"source_root": str(source_root), "prefix_root": str(prefix_root),
                    "before": before, "retained_prefix_candidates": len(retained),
                    "deferred_past_candidates": deferred,
                    "basis": "Preserve the earlier immutable research inputs; unrestricted new evidence retained, not denied or turned into zero",
                    "point_in_time_claimed": False},
                  published_at=datetime.now().astimezone().isoformat())
    folder = Path(destination) if destination else DATA_ROOT / "research/secondary_action_terms" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    shutil.copytree(source_root, folder)
    shutil.copy2(source_root / "result.json", folder / "unrestricted_source_result.json")
    shutil.copy2(prefix_root / "result.json", folder / "frozen_prefix_source_result.json")
    shutil.copy2(Path(__file__), folder / "prefix_freeze_implementation.py")
    write_json(folder / "result.json", output)
    return folder, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-view", type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--symbol", action="append")
    selection.add_argument("--source-root", type=Path, help="Publish an archived source while preserving earlier pinned completions")
    parser.add_argument("--freeze-prefix-root", type=Path)
    parser.add_argument("--freeze-before", help="Only newly complete terms on or after this date; older pinned terms stay unchanged")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--reuse-root", type=Path, help="Reuse completed raw histories with original observation times; query only new symbols")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("Explicit --execute needed for public source acquisition; no account action")
    if args.source_root:
        if not args.freeze_prefix_root or not args.freeze_before or args.event_view or args.reuse_root:
            parser.error("Prefix publication needs --source-root, --freeze-prefix-root and --freeze-before only")
        folder, result = freeze_prefix_terms(args.source_root, args.freeze_prefix_root, args.freeze_before, args.out)
    else:
        if not args.event_view or args.freeze_prefix_root or args.freeze_before:
            parser.error("Acquisition needs --event-view and --symbol, not prefix-publication options")
        folder, result = collect(args.event_view, args.symbol, args.out, reuse_root=args.reuse_root)
    print(json.dumps({"folder": str(folder), "status": result["status"],
                      "candidate_events": result["candidate_events"], "source_errors": result["source_errors"]}))
    return int(result["source_errors"] > 0)
