"""Resumable, research-only ETF history collection with separate source roots."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shutil
from threading import Lock
import time
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen

import msgpack
import pandas as pd

from ..settings import DATA_ROOT
from ..stockdb_source import ENDPOINT
from ..market_dataset import write_json


CURRENT_DAY = "20260924"
START = "20240101"
END = "20260924"
SINA_START = "20230101"
SOURCES = {"eastmoney-qfq": "AKShare fund_etf_hist_em / Eastmoney",
           "sina-ohlcv": "Sina public ETF history / installed AKShare decoder"}
COLUMNS = ("date", "open", "close", "high", "low", "volume", "amount",
           "amplitude", "pct_chg", "change", "turnover")


def current_fund_names(prefix, day=CURRENT_DAY, endpoint=ENDPOINT):
    if prefix not in {"1", "5"} or len(day) != 8:
        raise ValueError("Invalid fund-code query")
    params = {"cmd": "vals", "t": "日k", "k1": f"fwd:{prefix},{int(prefix) + 1}",
              "k2": f"key:{day}", "ap": "get.code,name"}
    with urlopen(endpoint + "?" + urlencode(params), timeout=20) as response:
        if response.headers.get_content_type() != "application/x-msgpack":
            raise ValueError("Unexpected StockDB response type")
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("Fund-name response is too large")
    rows = msgpack.unpackb(payload, raw=False)
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 for row in rows):
        raise ValueError("Unexpected fund-name rows")
    return payload, rows


def etf_candidates(rows):
    return sorted({code for code, name in rows if isinstance(code, str) and len(code) == 6
                   and isinstance(name, str) and "ETF" in name.upper() and "LOF" not in name.upper()})


def normalize_history(frame, symbol):
    if frame.empty or len(frame.columns) != len(COLUMNS):
        raise ValueError(f"No eleven-column ETF history for {symbol}")
    normalized = frame.copy()
    normalized.columns = COLUMNS
    normalized["date"] = pd.to_datetime(normalized.date, errors="raise").dt.strftime("%Y%m%d")
    if normalized.date.duplicated().any() or not normalized.date.between(START, END).all():
        raise ValueError(f"Invalid ETF dates for {symbol}")
    prices = normalized[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if (prices.isna().any().any() or prices.le(0).any().any()
            or prices.high.lt(prices[["open", "low", "close"]].max(axis=1)).any()
            or prices.low.gt(prices[["open", "high", "close"]].min(axis=1)).any()):
        raise ValueError(f"Invalid adjusted ETF OHLC for {symbol}")
    normalized["symbol"] = symbol
    return normalized.sort_values("date").reset_index(drop=True)


def fetch_one(symbol):
    import akshare as ak
    try:
        frame = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=START,
                                    end_date=END, adjust="qfq")
        return symbol, normalize_history(frame, symbol), None
    except Exception as error:
        return symbol, None, f"{type(error).__name__}: {error}"


def sina_payload(source):
    prefix = re.match(r"var\s+[A-Za-z_$][\w$]*\s*=\s*", source.strip())
    if prefix is None:
        raise ValueError("Unexpected Sina assignment prefix")
    remainder = source.strip()[prefix.end():]
    payload, end = json.JSONDecoder().raw_decode(remainder)
    if (not isinstance(payload, str)
            or not re.fullmatch(r";\s*(?:/\*[^*]*\*/)?", remainder[end:].strip())):
        raise ValueError("Unexpected Sina payload or trailing content")
    return payload


def normalize_sina_history(frame, symbol, start=SINA_START, end=END):
    columns = ("date", "open", "high", "low", "close", "volume")
    if frame.empty or not set(columns) <= set(frame.columns):
        raise ValueError(f"No six-column Sina history for {symbol}")
    normalized = frame.loc[:, list(columns)].copy()
    normalized["date"] = pd.to_datetime(normalized.date, errors="raise", utc=True).dt.strftime("%Y%m%d")
    numbers = normalized.loc[:, list(columns[1:])].apply(pd.to_numeric, errors="raise")
    if (normalized.date.duplicated().any() or normalized.date.isna().any()
            or not numbers.apply(lambda column: column.map(math.isfinite)).all().all()
            or numbers.volume.lt(0).any()):
        raise ValueError(f"Invalid Sina dates or numeric values for {symbol}")
    prices = numbers[["open", "high", "low", "close"]]
    if (prices.le(0).any().any() or prices.high.lt(prices.max(axis=1)).any()
            or prices.low.gt(prices.min(axis=1)).any()):
        raise ValueError(f"Invalid Sina OHLC for {symbol}")
    normalized.loc[:, list(columns[1:])] = numbers
    normalized = normalized.loc[normalized.date.between(start, end)].copy()
    if normalized.empty:
        raise ValueError(f"No Sina history in requested period for {symbol}")
    normalized["symbol"] = symbol
    return normalized.sort_values("date").reset_index(drop=True)


def fetch_sina_one(symbol, folder, start=SINA_START, end=END):
    import requests
    from akshare.fund.fund_etf_sina import hk_js_decode, py_mini_racer

    try:
        if not re.fullmatch(r"[15]\d{5}", symbol):
            raise ValueError("Expected a six-digit SH/SZ ETF candidate")
        code = ("sh" if symbol.startswith("5") else "sz") + symbol
        url = f"https://finance.sina.com.cn/realstock/company/{code}/hisdata_klc2/klc_kl.js"
        with requests.get(url, timeout=(5, 15), stream=True) as response:
            response.raise_for_status()
            parts, total = [], 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > 2_000_000:
                    raise ValueError("Sina history response exceeds 2 MB")
                parts.append(chunk)
            body = b"".join(parts)
            encoding = response.encoding or "utf-8"
        (Path(folder) / f"{symbol}_response.js").write_bytes(body)
        payload = sina_payload(body.decode(encoding))
        # Execute the installed decoder, never JavaScript received from the network.
        with py_mini_racer.MiniRacer() as decoder:
            decoder.eval(hk_js_decode, timeout_sec=3)
            rows = decoder.call("d", payload, timeout_sec=3)
        return symbol, normalize_sina_history(pd.DataFrame(rows), symbol, start, end), None
    except Exception as error:
        return symbol, None, f"{type(error).__name__}: {error}"


def create(folder=None, *, provider="eastmoney-qfq", master_folder=None):
    if provider not in SOURCES:
        raise ValueError("Unknown ETF history provider")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    category = "etf_adjusted" if provider == "eastmoney-qfq" else "etf_sina_history"
    folder = Path(folder) if folder else DATA_ROOT / "research" / category / run_id
    folder.mkdir(parents=True, exist_ok=False)
    if master_folder is not None:
        master_folder = Path(master_folder).resolve()
        parent = json.loads((master_folder / "master.json").read_text(encoding="utf-8"))
        parent_hash = sha256((master_folder / "master.json").read_bytes()).hexdigest()
        parent_state = json.loads((master_folder / "status.json").read_text(encoding="utf-8"))
        if parent_state["master_sha256"] != parent_hash:
            raise ValueError("Imported ETF master changed after collection began")
        master = dict(parent)
        for item in master["raw_sources"]:
            source = (master_folder / item["file"]).resolve()
            source.relative_to(master_folder)
            if sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Imported ETF master source hash changed")
            shutil.copyfile(source, folder / source.name)
        master["imported_master"] = {"folder": str(master_folder),
                                     "sha256": parent_hash}
        codes = master["candidate_codes"]
    else:
        all_rows, sources = [], []
        for prefix in ("1", "5"):
            payload, rows = current_fund_names(prefix)
            source_file = folder / f"stockdb_names_{prefix}.msgpack"
            source_file.write_bytes(payload)
            sources.append({"file": source_file.name, "sha256": sha256(payload).hexdigest(), "rows": len(rows)})
            all_rows.extend(rows)
        codes = etf_candidates(all_rows)
        master = {"source": "local StockDB current-day name marker", "date": CURRENT_DAY,
                  "fund_rows": len(all_rows), "candidate_codes": codes, "count": len(codes),
                  "raw_sources": sources, "historical_membership_validated": False,
                  "classification_warning": "ASCII ETF marker excludes LOF but is not an exchange-verified instrument master"}
    if not codes:
        raise ValueError("No current ETF candidates found")
    write_json(folder / "master.json", master)
    state = {"status": "collecting", "created_at": datetime.now().astimezone().isoformat(),
             "master_sha256": sha256((folder / "master.json").read_bytes()).hexdigest(),
             "provider": provider, "source": SOURCES[provider],
             "adjustment": "qfq" if provider == "eastmoney-qfq" else "provider_unverified",
             "start": START if provider == "eastmoney-qfq" else SINA_START,
             "end": END, "expected_symbols": len(codes), "receipts": {},
             "execution_admitted": False, "historical_membership_validated": False,
             "distribution_reconciliation_complete": False, "strategy_validated": False}
    if provider == "sina-ohlcv":
        state.update(adjustment_verified=False, volume_unit_verified=False,
                     amount_available=False, instrument_rules_validated=False)
    write_json(folder / "status.json", state)
    return folder


def collect(folder, max_symbols=100, workers=4, request_interval=1.0, transport_failure_limit=5):
    folder = Path(folder)
    if (max_symbols < 1 or not 1 <= workers <= 8 or not 0 <= request_interval <= 30
            or not 1 <= transport_failure_limit <= 20):
        raise ValueError("Invalid bounded ETF collection settings")
    master = json.loads((folder / "master.json").read_text(encoding="utf-8"))
    state = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    provider = state.get("provider", "eastmoney-qfq")
    if provider not in SOURCES:
        raise ValueError("Unknown saved ETF history provider")
    if provider == "sina-ohlcv" and (state["source"] != SOURCES[provider]
                                    or state["adjustment"] != "provider_unverified"):
        raise ValueError("Sina histories must remain in a separate unverified source collection")
    if sha256((folder / "master.json").read_bytes()).hexdigest() != state["master_sha256"]:
        raise ValueError("ETF master changed after collection began")
    pending = [code for code in master["candidate_codes"] if code not in state["receipts"]][:max_symbols]
    consecutive_failures, transport_stop = 0, None
    if pending:
        launch_lock, next_launch = Lock(), [0.0]

        def paced_fetch(code):
            with launch_lock:
                time.sleep(max(0.0, next_launch[0] - time.monotonic()))
                next_launch[0] = time.monotonic() + request_interval
            return (fetch_sina_one(code, folder, state["start"], state["end"])
                    if provider == "sina-ohlcv" else fetch_one(code))

        queue = iter(pending)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            active = set()
            for _ in range(min(workers, len(pending))):
                active.add(pool.submit(paced_fetch, next(queue)))
            while active:
                done, active = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    code, frame, error = future.result()
                    if error:
                        state["receipts"][code] = {"status": "failed_no_automatic_retry", "error": error}
                        network_error = error.startswith(("ProxyError:", "ReadTimeout:", "ConnectTimeout:",
                                                           "Timeout:", "SSLError:", "ConnectionError:"))
                        consecutive_failures = consecutive_failures + 1 if network_error else 0
                    else:
                        consecutive_failures = 0
                        path = folder / f"{code}.parquet"
                        frame.to_parquet(path, index=False)
                        state["receipts"][code] = {"status": "acquired_unvalidated", "file": path.name,
                                                   "sha256": sha256(path.read_bytes()).hexdigest(),
                                                   "rows": len(frame), "first_date": frame.date.iloc[0],
                                                   "last_date": frame.date.iloc[-1]}
                    if consecutive_failures >= transport_failure_limit and transport_stop is None:
                        transport_stop = {"stopped_at": datetime.now().astimezone().isoformat(),
                                          "reason": "consecutive_transport_failures", "last_error": error,
                                          "limit": transport_failure_limit, "workers": workers,
                                          "request_interval_seconds": request_interval}
                        state.setdefault("transport_stops", []).append(transport_stop)
                    if provider == "sina-ohlcv":
                        source_file = folder / f"{code}_response.js"
                        if source_file.exists():
                            state["receipts"][code].update(raw_file=source_file.name,
                                                          raw_sha256=sha256(source_file.read_bytes()).hexdigest())
                        state["receipts"][code]["retrieved_at"] = datetime.now().astimezone().isoformat()
                    write_json(folder / "status.json", state)
                    print(f"{len(state['receipts'])}/{len(master['candidate_codes'])} {code}: {state['receipts'][code]['status']}", flush=True)
                # Drain already-started requests, but leave unstarted codes pending.
                if transport_stop is None:
                    for _ in range(workers - len(active)):
                        code = next(queue, None)
                        if code is None:
                            break
                        active.add(pool.submit(paced_fetch, code))
    success = sum(row["status"] == "acquired_unvalidated" for row in state["receipts"].values())
    failed = sum(row["status"] == "failed_no_automatic_retry" for row in state["receipts"].values())
    state.update(acquired_symbols=success, failed_symbols=failed,
                 pending_symbols=len(master["candidate_codes"]) - len(state["receipts"]),
                 updated_at=datetime.now().astimezone().isoformat())
    state["status"] = "all_attempted_with_failures" if state["pending_symbols"] == 0 and failed else (
        "all_acquired_unvalidated" if state["pending_symbols"] == 0 else "partial_backfill")
    if transport_stop is not None and state["pending_symbols"]:
        state["status"] = "transport_interrupted_backfill"
    write_json(folder / "status.json", state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, help="Resume an existing immutable-master collection")
    parser.add_argument("--provider", choices=SOURCES, default=None, help="New collection source; never mix sources")
    parser.add_argument("--master-folder", type=Path, help="Reuse a saved candidate master in a new collection")
    parser.add_argument("--max-symbols", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--request-interval", type=float, default=1.0, help="Minimum spacing between request starts")
    parser.add_argument("--transport-failure-limit", type=int, default=5, help="Stop scheduling after consecutive network failures")
    args = parser.parse_args(argv)
    if args.folder and (args.provider or args.master_folder):
        parser.error("Resume with --folder alone; provider/master are pinned at creation")
    folder = args.folder or create(provider=args.provider or "eastmoney-qfq", master_folder=args.master_folder)
    state = collect(folder, args.max_symbols, args.workers, args.request_interval, args.transport_failure_limit)
    print(json.dumps({"folder": str(folder), "status": state["status"],
                      "acquired": state["acquired_symbols"], "failed": state["failed_symbols"],
                      "pending": state["pending_symbols"]}, ensure_ascii=False))
    return 0 if state["failed_symbols"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
