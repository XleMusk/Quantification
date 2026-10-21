"""Bounded raw daily source candidates, reconciled but never silently promoted."""
import argparse
from datetime import date, datetime
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import requests

from a_stock_committee.market_data import normalize_symbol
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from ..worker import lock
from .experiment import write_json


ENDPOINT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SOURCE_CODE = "https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py"
EASTMONEY_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_CODE = "https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_em.py"
EASTMONEY_DOC = "https://akshare.akfamily.xyz/data/stock/stock.html"
FIELDS = ["open", "high", "low", "close", "volume_shares_candidate", "amount_cny_candidate", "turn_provider_pct"]
LIMITS = [
    "Retrieval-time Tencent candidates, not an extension admitted to the existing account or verified PIT",
    "Raw positional fields follow the reviewed upstream implementation; original response is retained",
    "Ordinary-stock volume provisionally uses lots*100 except STAR shares; amount provisionally uses wan RMB*10000",
    "Unit checks against cached reference and OHLC consistency are not independent exchange-unit verification",
    "The installed older AKShare amount column is not used as RMB turnover; the shared environment is unchanged",
    "Missing preclose, suspension/ST status, listing/action/reference/financial fields remain unknown, never zero",
    "Missing candidate dates are not assumed suspended or zero-return; delisted/unsupported securities stay explicit",
    "No old quote replacement, account replay, source blacklist retry, model/parameter changes or broker execution; NO_TRADE",
]
EASTMONEY_LIMITS = [
    "Retrieval-time Eastmoney raw daily candidates; not a current/PIT trading universe or admitted account feed",
    "Requested fqt0/daily101; original response bytes, request parameters and source identity are retained",
    "AKShare upstream docs declare all-stock volume in lots and amount in CNY; normalized volume uses lots*100",
    "Provider field documentation and reference consistency do not independently verify every exchange record",
    "Missing preclose/ST/suspension/listing/action/reference data are unknown; dates are not synthesized",
    "No inferred adjustment factors, current financial backfill, old quote/account replacement or strategy changes",
    "Failed requests stop without retries, proxy/account changes or resume of another failed provider; NO_TRADE",
]


def decode_raw(text, symbol, start, end):
    source_symbol = ("sh" if symbol.endswith(".SSE") else "sz") + symbol[:6]
    prefix = "kline_day="
    if not text.startswith(prefix):
        raise ValueError("Unexpected Tencent assignment envelope")
    payload = json.loads(text[len(prefix):])
    if payload.get("code") != 0:
        raise ValueError("Tencent returned a source error: " + str(payload.get("msg")))
    security = payload["data"][source_symbol]
    if "day" not in security:
        raise ValueError("Raw day rows absent; adjusted-price fallback is forbidden")
    rows = security["day"]
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 9 for row in rows):
        raise ValueError("Tencent daily positional schema changed")
    board = stock_board(symbol)
    frame = pd.DataFrame([[row[i] for i in [0, 1, 3, 4, 2, 5, 8, 7]] for row in rows], columns=["date", *FIELDS])
    if (frame.date.duplicated().any() or any(date.fromisoformat(day).isoformat() != day for day in frame.date)):
        raise ValueError("Daily date identities are invalid or duplicated")
    observed = {"raw_rows_received": len(frame), "raw_first_day": frame.date.min() if len(frame) else None,
                "raw_last_day": frame.date.max() if len(frame) else None}
    frame = frame.loc[frame.date.between(start, end)].sort_values("date").reset_index(drop=True)
    for field in FIELDS:
        frame[field] = pd.to_numeric(frame[field].replace("", np.nan), errors="raise")
    frame["volume_shares_candidate"] *= 1 if board == "star" else 100
    frame["amount_cny_candidate"] *= 10000
    frame["symbol"] = symbol
    frame.attrs.update(observed)
    return frame


def decode_eastmoney_raw(text, symbol, start, end):
    stock_board(symbol)
    payload = json.loads(text)
    if payload.get("rc") != 0 or "data" not in payload:
        raise ValueError("Eastmoney daily source error or envelope changed")
    security = payload["data"]
    if security is None:
        source_rows, source_state = [], "explicit_null_data"
    else:
        market = 1 if symbol.endswith(".SSE") else 0
        if (not isinstance(security, dict) or security.get("code") != symbol[:6]
                or security.get("market") != market or not isinstance(security.get("klines"), list)):
            raise ValueError("Eastmoney raw daily identity/schema changed")
        source_rows, source_state = security["klines"], "observed_security"
    if any(not isinstance(row, str) or len(row.split(",")) != 11 for row in source_rows):
        raise ValueError("Eastmoney daily positional schema changed")
    records = [row.split(",") for row in source_rows]
    frame = pd.DataFrame([[row[i] for i in (0, 1, 3, 4, 2, 5, 6, 10)] for row in records],
                         columns=["date", *FIELDS])
    if (frame.date.duplicated().any() or any(date.fromisoformat(day).isoformat() != day for day in frame.date)):
        raise ValueError("Daily date identities are invalid or duplicated")
    observed = {"raw_rows_received": len(frame), "raw_first_day": frame.date.min() if len(frame) else None,
                "raw_last_day": frame.date.max() if len(frame) else None, "provider_data_state": source_state}
    frame = frame.loc[frame.date.between(start, end)].sort_values("date").reset_index(drop=True)
    for field in FIELDS:
        frame[field] = pd.to_numeric(frame[field].replace("", np.nan), errors="raise")
    frame["volume_shares_candidate"] *= 100
    frame["symbol"] = symbol
    frame.attrs.update(observed)
    return frame


def quality(frame):
    values = frame[FIELDS]
    finite = np.isfinite(values.to_numpy()).all(axis=1)
    prices = frame[["open", "high", "low", "close"]]
    positive = prices.gt(0).all(axis=1)
    ohlc = frame.high.ge(prices.max(axis=1)) & frame.low.le(prices.min(axis=1))
    activity = frame.volume_shares_candidate.ge(0) & frame.amount_cny_candidate.ge(0) & frame.turn_provider_pct.ge(0)
    vwap = frame.amount_cny_candidate / frame.volume_shares_candidate.where(frame.volume_shares_candidate.gt(0))
    inside = vwap.between(frame.low - .011, frame.high + .011)
    inactive = frame.volume_shares_candidate.eq(0) & frame.amount_cny_candidate.eq(0)
    return {"rows": len(frame), "nonfinite_rows": int((~finite).sum()),
            "invalid_price_ohlc_or_activity_rows": int((~(positive & ohlc & activity)).sum()),
            "amount_volume_outside_ohlc_rows": int((~(inside | inactive)).sum()),
            "latest_observed_day": frame.date.max() if len(frame) else None,
            "source_units_independently_verified": False}


def compare(frame, reference):
    matched = frame.merge(reference, on=["date", "symbol"], how="outer", suffixes=("_candidate", "_reference"), indicator=True,
                          validate="one_to_one")
    both = matched._merge.eq("both")
    price_gaps = pd.DataFrame({field: (matched[field + "_candidate"] - matched[field + "_reference"]).abs()
                              for field in ("open", "high", "low", "close")})
    matched["max_ohlc_abs_gap"] = price_gaps.max(axis=1).where(both)
    matched["volume_abs_gap"] = (matched.volume_shares_candidate - matched.volume).abs().where(both)
    matched["volume_candidate_reference_ratio"] = (matched.volume_shares_candidate / matched.volume.where(matched.volume.gt(0))).where(both)
    matched["amount_abs_gap"] = (matched.amount_cny_candidate - matched.amount).abs().where(both)
    matched["amount_relative_abs_gap"] = (matched.amount_abs_gap / matched.amount.where(matched.amount.gt(0))).where(both)
    paired = matched.loc[both]
    summary = {"paired_rows": len(paired), "candidate_only_rows": int(matched._merge.eq("left_only").sum()),
               "reference_only_rows": int(matched._merge.eq("right_only").sum()),
               "exact_ohlc_rows": int(paired.max_ohlc_abs_gap.le(1e-8).sum()),
               "max_ohlc_abs_gap": paired.max_ohlc_abs_gap.max(), "exact_volume_rows": int(paired.volume_abs_gap.eq(0).sum()),
               "max_volume_abs_gap": paired.volume_abs_gap.max(),
               "median_volume_ratio": paired.volume_candidate_reference_ratio.median(),
               "max_amount_relative_abs_gap": paired.amount_relative_abs_gap.max()}
    return matched, summary


def create(universe, reference_view, start, end, priority=(), *, provider="tencent"):
    if provider not in {"tencent", "eastmoney"}:
        raise ValueError("Unsupported alternate daily provider")
    max_span = 1461 if provider == "eastmoney" else 366
    if not start <= end <= date.today().isoformat() or (date.fromisoformat(end) - date.fromisoformat(start)).days > max_span:
        raise ValueError(f"Use canonical past/current dates spanning at most{max_span} days")
    for day in (start, end):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Canonical ISO source dates required")
    raw_file = Path(reference_view).resolve() / "raw_daily.parquet"
    if not raw_file.is_file():
        raise ValueError("Existing raw daily reference required")
    master = pd.read_csv(universe, dtype=str, keep_default_na=False)
    symbols, unsupported = [], []
    for code in master.loc[master.type.eq("1"), "code"]:
        symbol = normalize_symbol(code).vt_symbol
        try:
            stock_board(symbol)
        except ValueError as error:
            unsupported.append({"symbol": symbol, "reason": str(error)})
        else:
            symbols.append(symbol)
    symbols = sorted(set(symbols))
    priority = [normalize_symbol(symbol).vt_symbol for symbol in priority]
    if len(set(priority)) != len(priority) or not set(priority) <= set(symbols) or not symbols:
        raise ValueError("Priority securities must be distinct members of the existing supported full source")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/alternate_daily" / identifier
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    (root / "normalized").mkdir()
    (root / "comparisons").mkdir()
    shutil.copy2(__file__, root / "implementation.py")
    plan = {"id": identifier, "created_at": datetime.now().astimezone().isoformat(), "provider": provider,
            "source": EASTMONEY_ENDPOINT if provider == "eastmoney" else ENDPOINT,
            "source_code": EASTMONEY_CODE if provider == "eastmoney" else SOURCE_CODE,
            "installed_akshare_version": version("akshare"), "universe": str(Path(universe).resolve()),
            "reference_file": str(raw_file), "start": start, "end": end,
            "symbols": priority + [symbol for symbol in symbols if symbol not in priority], "unsupported": unsupported,
            "execution_admitted": False, "limits": EASTMONEY_LIMITS if provider == "eastmoney" else LIMITS}
    if provider == "eastmoney":
        plan.update(source_document=EASTMONEY_DOC, adjustment="raw_fqt0", frequency="daily101")
    write_json(root / "plan.json", plan)
    write_json(root / "result.json", {"id": identifier, "status": "prepared", "entries": {}, "execution_admitted": False})
    return root


def coverage_table(plan, result):
    rows = []
    for symbol in plan["symbols"]:
        entry = result["entries"].get(symbol)
        row = {"symbol": symbol, "board": stock_board(symbol), "status": "pending"}
        if entry:
            check, paired = entry["quality"], entry["comparison"]
            row.update(status="data_in_requested_range" if check["rows"] else "no_rows_in_requested_range",
                       rows=check["rows"], latest_observed_day=check["latest_observed_day"],
                       raw_rows_received=entry.get("raw_observation", {}).get("raw_rows_received"), **paired,
                       nonfinite_rows=check["nonfinite_rows"],
                       invalid_price_ohlc_or_activity_rows=check["invalid_price_ohlc_or_activity_rows"],
                       amount_volume_outside_ohlc_rows=check["amount_volume_outside_ohlc_rows"])
        elif result.get("failed_symbol") == symbol:
            row["status"] = "failed_source_request"
        rows.append(row)
    frame = pd.DataFrame(rows)
    counts = {"source_scope_stocks": len(frame), "queried_stocks": len(result["entries"]),
              "stocks_with_data": int(frame.status.eq("data_in_requested_range").sum()),
              "stocks_without_requested_rows": int(frame.status.eq("no_rows_in_requested_range").sum()),
              "pending_stocks": int(frame.status.eq("pending").sum()),
              "failed_stocks": int(frame.status.eq("failed_source_request").sum())}
    for field in ("rows", "paired_rows", "exact_ohlc_rows", "candidate_only_rows", "reference_only_rows",
                  "nonfinite_rows", "invalid_price_ohlc_or_activity_rows", "amount_volume_outside_ohlc_rows"):
        counts[field] = int(frame[field].sum()) if field in frame else 0
    counts["price_conflict_rows"] = counts["paired_rows"] - counts["exact_ohlc_rows"]
    counts["execution_admitted"] = False
    return frame, counts


def report(root, plan, result):
    out = ROOT / "outputs/alternate_daily" / plan["id"]
    out.mkdir(parents=True, exist_ok=True)
    coverage, counts = coverage_table(plan, result)
    coverage.to_csv(root / "candidate_index.csv", index=False)
    write_json(root / "coverage.json", counts)
    write_json(out / "run.json", result | {"artifacts": str(root), "plan": str(root / "plan.json"), "coverage": counts})
    lines = ["# Alternate Raw Daily Candidate Cache", "", "Status: " + result["status"] + ". NO_TRADE.",
             f"Requested source identities: {counts['source_scope_stocks']}; queries cached: {counts['queried_stocks']}; pending: {counts['pending_stocks']}; failed: {counts['failed_stocks']}.",
             f"Stocks with requested rows: {counts['stocks_with_data']}; no rows in requested range: {counts['stocks_without_requested_rows']} (not inferred suspended or delisted).",
             f"Requested range: {plan['start']} to {plan['end']}. No original quote/ledger replacement.", "",
             f"Rows: {counts['rows']}; paired: {counts['paired_rows']}; exact OHLC: {counts['exact_ohlc_rows']}; price conflicts: {counts['price_conflict_rows']}.",
             f"Candidate-only rows: {counts['candidate_only_rows']}; reference-only rows: {counts['reference_only_rows']}. Neither is imputed.",
             f"Nonfinite rows: {counts['nonfinite_rows']}; invalid price/OHLC/activity: {counts['invalid_price_ohlc_or_activity_rows']}; VWAP outside OHLC: {counts['amount_volume_outside_ohlc_rows']}.", "",
             "|Board|Source identities|Queried with data|Queried without rows|Pending|Failed|",
             "|---|---:|---:|---:|---:|---:|"]
    for board, members in coverage.groupby("board", sort=True):
        values = [int(members.status.eq(state).sum()) for state in
                  ("data_in_requested_range", "no_rows_in_requested_range", "pending", "failed_source_request")]
        lines.append(f"|{board}|{len(members)}|" + "|".join(map(str, values)) + "|")
    lines += ["", "Full per-security data availability and measured differences: " + str(root / "candidate_index.csv"),
              "Universe includes historical source identities; it is not a current/PIT eligible trading universe."]
    lines += ["", *["- " + value for value in plan["limits"]], "", "Upstream field implementation: " + plan["source_code"],
              "Artifacts: " + str(root)]
    if result.get("error"):
        lines.append("Failure: " + result["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect(root, max_queries=64, request_get=requests.get):
    if not 1 <= max_queries <= 256:
        raise ValueError("Use a bounded1..256-query batch")
    root = Path(root).resolve()
    with lock(root / "collector.lock"):
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        result = json.loads((root / "result.json").read_text(encoding="utf-8"))
        provider = plan.get("provider", "tencent")
        expected = EASTMONEY_ENDPOINT if provider == "eastmoney" else ENDPOINT
        if (provider not in {"tencent", "eastmoney"} or plan["source"] != expected
                or result["execution_admitted"] is not False):
            raise ValueError("Unadmitted source-specific candidate cache required")
        if provider == "eastmoney" and (plan.get("adjustment") != "raw_fqt0" or plan.get("frequency") != "daily101"):
            raise ValueError("Only the explicitly raw Eastmoney daily source is supported")
        if result["status"] == "failed_source_request":
            raise ValueError("Failed source requests are retained, not automatically retried")
        wanted = [symbol for symbol in plan["symbols"] if symbol not in result["entries"]][:max_queries]
        codes = [("sh." if symbol.endswith(".SSE") else "sz.") + symbol[:6] for symbol in wanted]
        reference = pq.read_table(plan["reference_file"], columns=["date", "code", "open", "high", "low", "close", "volume", "amount", "tradestatus", "isST"],
            filters=[("date", ">=", plan["start"]), ("date", "<=", plan["end"]), ("code", "in", codes)]).to_pandas()
        reference["symbol"] = reference.code.map(lambda code: normalize_symbol(code).vt_symbol)
        for field in ("open", "high", "low", "close", "volume", "amount"):
            reference[field] = pd.to_numeric(reference[field], errors="coerce")
        result.update(status="collecting", batch_started_at=datetime.now().astimezone().isoformat())
        write_json(root / "result.json", result)
        current = None
        try:
            for current in wanted:
                if provider == "eastmoney":
                    params = {"fields1": "f1,f2,f3,f4,f5,f6",
                              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                              "ut": "7eea3edcaed734bea9cbfc24409ed989", "klt": "101", "fqt": "0",
                              "secid": ("1." if current.endswith(".SSE") else "0.") + current[:6],
                              "beg": plan["start"].replace("-", ""), "end": plan["end"].replace("-", "")}
                else:
                    source_symbol = ("sh" if current.endswith(".SSE") else "sz") + current[:6]
                    params = {"_var": "kline_day", "param": f"{source_symbol},day,{plan['start']},{plan['end']},640,"}
                result["pending_request"] = {"symbol": current, "source": plan["source"], "params": params}
                write_json(root / "result.json", result)
                response = request_get(plan["source"], params=params, timeout=(5, 12))
                if provider == "eastmoney":
                    (root / "raw" / (current + ".txt")).write_bytes(response.content)
                else:
                    (root / "raw" / (current + ".txt")).write_text(response.text, encoding="utf-8")
                response.raise_for_status()
                decode = decode_eastmoney_raw if provider == "eastmoney" else decode_raw
                frame = decode(response.text, current, plan["start"], plan["end"])
                normalized = "normalized/" + current + ".parquet"
                frame.to_parquet(root / normalized, index=False)
                paired, comparison = compare(frame, reference.loc[reference.symbol.eq(current)].drop(columns="code"))
                paired.to_csv(root / "comparisons" / (current + ".csv"), index=False)
                entry = {"received_at": datetime.now().astimezone().isoformat(), "response_url": response.url, "params": params,
                         "file": normalized, "raw_observation": frame.attrs, "quality": quality(frame), "comparison": comparison}
                result["entries"][current] = entry
                result.pop("pending_request", None)
                write_json(root / "result.json", result)
                print(f"{current}: rows={len(frame)}, paired={comparison['paired_rows']}, "
                      f"OHLC exact={comparison['exact_ohlc_rows']}, volume ratio={comparison['median_volume_ratio']}", flush=True)
                time.sleep(.25)
            result["status"] = "completed_candidate_cache" if len(result["entries"]) == len(plan["symbols"]) else "partial_candidate_cache"
        except Exception as error:
            result.update(status="failed_source_request", failed_symbol=current, error=f"{type(error).__name__}: {error}")
        finally:
            result["pending_queries"] = len(plan["symbols"]) - len(result["entries"])
            result["batch_finished_at"] = datetime.now().astimezone().isoformat()
            write_json(root / "result.json", result)
            report(root, plan, result)
        print(str(root), flush=True)
        return root, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--priority", default="")
    parser.add_argument("--max-queries", type=int, default=64)
    parser.add_argument("--provider", choices=["tencent", "eastmoney"])
    args = parser.parse_args(argv)
    if args.resume:
        if any((args.universe, args.reference_view, args.start, args.end, args.priority, args.provider)):
            parser.error("Resume reuses the original complete source scope and dates")
        root = args.resume
    else:
        if not all((args.universe, args.reference_view, args.start, args.end)):
            parser.error("New collection needs universe/reference-view/start/end")
        root = create(args.universe, args.reference_view, args.start, args.end,
                      [value for value in args.priority.split(",") if value], provider=args.provider or "tencent")
    _, result = collect(root, args.max_queries)
    return 1 if result["status"] == "failed_source_request" else 0


if __name__ == "__main__":
    raise SystemExit(main())
