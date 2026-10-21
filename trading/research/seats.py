"""Read-only Eastmoney disclosure collection and conservative seat normalization."""
import argparse
from datetime import datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd
import requests

from ..settings import DATA_ROOT

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = {
    "summary": "RPT_DAILYBILLBOARD_DETAILSNEW",
    "buy": "RPT_BILLBOARD_DAILYDETAILSBUY",
    "sell": "RPT_BILLBOARD_DAILYDETAILSSELL",
}
DEFAULT_HOME = DATA_ROOT / "research" / "seat_trend"
INVESTOR_CATEGORY_AGGREGATES = frozenset({"自然人", "中小投资者", "其他自然人", "机构投资者", "深股通投资者"})


def is_investor_category_aggregate(name):
    """Known overlapping statistical categories, never brokerage identities."""
    return isinstance(name, str) and name.strip() in INVESTOR_CATEGORY_AGGREGATES


def derived_seat_entity(name, reported_identity_usable):
    """Separate consumer eligibility from the unchanged saved source flag.

    Do not use these fields to rewrite original event keys: the same category
    statistic can repeat on both boards without becoming an identified seat.
    """
    category = is_investor_category_aggregate(name)
    usable = reported_identity_usable in (True, 1, "1")
    return {"entity_kind": "investor_category_aggregate" if category else
            "reported_named_seat_not_person_verified" if usable else "unidentified_or_generic_seat",
            "seat_identity_usable": bool(usable and not category)}


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def iso_day(value):
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def collect(folder, start, end, codes=(), max_pages=80, interval=.7):
    """Cache immutable raw pages; completeness means provider pagination, not exchange audit."""
    start, end = iso_day(start), iso_day(end)
    if start > end or not 1 <= max_pages <= 100 or interval < .5:
        raise ValueError("Invalid bounded collection settings")
    codes = sorted(set(codes))
    if any(not re.fullmatch(r"\d{6}", c) for c in codes):
        raise ValueError("Expected six-digit security codes")
    where = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    if codes:
        where += '(SECURITY_CODE in (' + ','.join(f'"{c}"' for c in codes) + '))'
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    query = {"source": "Eastmoney", "url": URL, "start": start, "end": end, "codes": codes,
             "pagination_version": 2}
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old["query"] != query:
            raise ValueError("Refusing to mix queries or pagination versions; use a new snapshot folder")
    manifest = {"query": query, "status": "collecting", "reports": {},
                "publication_timestamps_available": False, "completeness": "provider_pages_only"}
    save_json(manifest_path, manifest)
    try:
        with requests.Session() as session:
            for kind, report in REPORTS.items():
                total, pages, count = 0, 1, None
                for page in range(1, max_pages + 1):
                    sort_columns = "TRADE_DATE,SECURITY_CODE"
                    if kind != "summary":
                        sort_columns += ",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL"
                    params = {"reportName": report, "columns": "ALL", "filter": where,
                              "pageSize": 500, "pageNumber": page,
                              "sortColumns": sort_columns,
                              "sortTypes": ",".join("1" for _ in sort_columns.split(",")),
                              "source": "WEB", "client": "WEB"}
                    target = folder / f"{kind}_{page:04d}.json.gz"
                    if target.exists():
                        with gzip.open(target, "rt", encoding="utf-8") as stream:
                            document = json.load(stream)
                        if document["request"] != params:
                            raise ValueError("Cached request identity mismatch")
                        response = document["response"]
                    else:
                        time.sleep(interval)
                        http = session.get(URL, params=params, timeout=(10, 45))
                        http.raise_for_status()
                        response = http.json()
                        if response.get("success") is not True or not isinstance(response.get("result"), dict):
                            raise ValueError(f"{kind}: empty/failed response is not verified absence: {response.get('message')}")
                        document = {"request": params, "response": response,
                                    "retrieved_at": datetime.now().astimezone().isoformat(),
                                    "published_at": None}
                        temporary = target.with_suffix(".tmp")
                        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
                            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
                        temporary.replace(target)
                    result = response["result"]
                    if page == 1:
                        pages, count = int(result["pages"]), int(result["count"])
                        if pages > max_pages:
                            raise ValueError(f"{kind}: {pages} pages exceeds explicit budget {max_pages}")
                    elif pages != int(result["pages"]) or count != int(result["count"]):
                        raise ValueError("Pagination changed during collection; use a new snapshot")
                    rows = result["data"]
                    if not rows:
                        raise ValueError("Empty page is not a verified no-event day")
                    for row in rows:
                        day, code = row["TRADE_DATE"][:10], row["SECURITY_CODE"]
                        if not start <= day <= end or (codes and code not in codes):
                            raise ValueError("Source ignored requested date/security filter")
                    total += len(rows)
                    print(f"{kind}: page {page}/{pages}, rows {total}/{count}", flush=True)
                    if page == pages:
                        break
                if total != count:
                    raise ValueError("Incomplete pagination")
                manifest["reports"][kind] = {"pages": pages, "rows": total}
                save_json(manifest_path, manifest)
        manifest.update(status="ready", completed_at=datetime.now().astimezone().isoformat())
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        save_json(manifest_path, manifest)
        raise
    save_json(manifest_path, manifest)
    return manifest


def read_raw(folder, kind):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "ready":
        raise ValueError("Cannot analyze an incomplete collection")
    records = []
    for page in range(1, manifest["reports"][kind]["pages"] + 1):
        with gzip.open(folder / f"{kind}_{page:04d}.json.gz", "rt", encoding="utf-8") as stream:
            document = json.load(stream)
        for index, row in enumerate(document["response"]["result"]["data"]):
            records.append({**row, "_raw_id": f"{kind}:{page}:{index}", "_retrieved_at": document["retrieved_at"]})
    if len(records) != manifest["reports"][kind]["rows"]:
        raise ValueError("Raw row count changed")
    return records


def period(reason):
    # 'Within three sessions' does not identify the actual start of an abnormal window.
    if re.search(r"三个交易日|三日|3个交易日|3日", reason):
        return "multi_up_to_3", None
    if re.search(r"连续|累计|异常波动|无价格|退市整理", reason):
        return "unknown_or_multi", None
    if re.search(r"日涨|日跌|日振幅|日换手|当日|涨幅偏离值|跌幅偏离值|振幅值|换手率", reason):
        return "single", 1
    return "unknown_or_multi", None


def money_cny(value, unit):
    factors = {"CNY": 1., "thousand_CNY": 1000., "ten_thousand_CNY": 10000.}
    if unit not in factors:
        raise ValueError("Unknown amount unit")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Missing/invalid disclosed amount") from error
    if not np.isfinite(number) or number < 0:
        raise ValueError("Missing/negative/nonfinite disclosed amount")
    return number * factors[unit]


def observations(buy, sell):
    """No aliases, no person identity, no vendor future-return/probability fields."""
    records = []
    for direction, rows in (("buy", buy), ("sell", sell)):
        for raw in rows:
            day = raw["TRADE_DATE"][:10]
            reason = raw["EXPLANATION"] or ""
            kind, sessions = period(reason)
            name = raw["OPERATEDEPT_NAME"]
            code = str(raw.get("OPERATEDEPT_CODE") or "")
            generic = (not name or code in {"", "0"} or
                       any(word in name for word in ("机构专用", "交易专用", "股通专用")))
            def known_amount(field):
                try:
                    return money_cny(raw[field], "CNY")
                except (TypeError, ValueError):
                    return np.nan
            # A missing opposite side must not erase the amount that WAS disclosed.
            b, s, amount = (known_amount(field) for field in ("BUY", "SELL", "ACCUM_AMOUNT"))
            try:
                reported_net = float(raw["NET"])
            except (TypeError, ValueError):
                reported_net = np.nan
            valid = bool(np.isfinite([b, s, amount, reported_net]).all() and amount > 0
                         and abs((b - s) - reported_net) <= .02 and max(b, s) <= amount * 1.001)
            symbol = raw["SECUCODE"].replace(".SH", ".SSE").replace(".SZ", ".SZSE")
            record = {"trade_date": day, "symbol": symbol, "seat_name": name, "seat_code": code,
                      "window_kind": kind, "window_start": day if sessions == 1 else None,
                      "window_end": day, "window_sessions": sessions, "reason": reason,
                      "buy_cny": b, "sell_cny": s, "net_cny": b-s,
                      "period_turnover_cny": amount, "source_unit": "CNY",
                      "listed_side": direction, "trade_id": str(raw.get("TRADE_ID") or ""),
                      "raw_id": raw["_raw_id"], "retrieved_at": raw["_retrieved_at"],
                      "published_at": None, "source": "Eastmoney", "identity_usable": not generic,
                      "amount_valid": valid, "person_verified": False}
            # Preserve identity_usable as the existing reported-normalization
            # field (and thus original event keys); consumers use this overlay.
            record.update(derived_seat_entity(name, record["identity_usable"]))
            records.append(record)
    return records


def normalize(buy, sell):
    data = pd.DataFrame(observations(buy, sell))
    if data.empty:
        raise ValueError("No seat records to normalize")
    # Generic seats remain individual observations. They are never aggregated as one investor.
    data["identity_key"] = data.seat_name.where(data.identity_usable & data.amount_valid, data.raw_id)
    key = ["trade_date", "symbol", "window_kind", "identity_key", "seat_code", "buy_cny", "sell_cny", "period_turnover_cny"]
    # Unknown windows cannot be safely deduplicated across different reasons.
    data["unknown_reason_key"] = data.reason.where(data.window_kind.ne("single"), "")
    key += ["unknown_reason_key"]
    groups = data.groupby(key, dropna=False, sort=False)
    result = groups.agg({**{c: "first" for c in data.columns if c not in key},
                         "listed_side": lambda x: "|".join(sorted(set(x))),
                         "reason": lambda x: " | ".join(sorted(set(x))),
                         "raw_id": lambda x: "|".join(x)}).reset_index()
    conflicts = result.loc[result.seat_identity_usable & result.amount_valid & result.window_kind.eq("single")].groupby(
        ["trade_date", "symbol", "seat_name", "seat_code"]).size()
    conflict_keys = set(conflicts[conflicts > 1].index)
    result["amount_conflict"] = [tuple(row) in conflict_keys for row in result[["trade_date", "symbol", "seat_name", "seat_code"]].values]
    result["eligible"] = (result.seat_identity_usable & result.amount_valid & ~result.amount_conflict & result.window_kind.eq("single"))
    result["event_id"] = [sha256(json.dumps([str(row[k]) for k in key], ensure_ascii=False).encode()).hexdigest()[:20]
                          for row in result.to_dict("records")]
    # Derive after the unchanged event-id recipe. Legacy consumers querying
    # identity_usable also see the corrected eligibility, while the old flag
    # remains explicit rather than being erased or rewritten in the source.
    result["reported_identity_usable"] = result["identity_usable"]
    result["identity_usable"] = result["seat_identity_usable"]
    result = result.drop(columns=["identity_key", "unknown_reason_key"]).sort_values(["trade_date", "symbol", "seat_name"])
    return result.reset_index(drop=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=iso_day, default="2024-09-26")
    parser.add_argument("--end", type=iso_day, default="2026-09-24")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--codes", default="")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"start": args.start, "end": args.end, "codes": args.codes,
                          "out": str(args.out), "network": False}, ensure_ascii=False))
        return 0
    collect(args.out, args.start, args.end, args.codes.split(",") if args.codes else (), args.max_pages)
    frame = normalize(read_raw(args.out, "buy"), read_raw(args.out, "sell"))
    frame.to_parquet(args.out / "events.parquet", index=False)
    save_json(args.out / "normalization.json", {"events": len(frame), "eligible_single_seat_events": int(frame.eligible.sum()),
        "symbol_days": len(frame[["symbol", "trade_date"]].drop_duplicates()),
        "window_counts": {str(k): int(v) for k, v in frame.window_kind.value_counts().items()},
        "raw_future_performance_fields_used": False, "publication_timestamps_available": False})
    print(f"Normalized {len(frame)} disclosures; eligible single-day seats {frame.eligible.sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
