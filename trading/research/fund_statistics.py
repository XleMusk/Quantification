"""Spot-check a frozen raw fund panel against public SSE daily statistics."""

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import uuid

import pandas as pd
import requests

from .fund_disclosures import fetch, parameters as disclosure_parameters
from ..market_dataset import write_json
from ..settings import DATA_ROOT


SQL_ID = "COMMON_SSE_CP_GPJCTPZ_GPLB_CJGK_MRGK_C"
RECIPE = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_fund_2021.js"
# The daily statistics are rounded, not mill-precision historical quotes.
FIELDS = {
    "open": ("OPEN_PRICE", Decimal(1), Decimal("0.001")),
    "close": ("CLOSE_PRICE", Decimal(1), Decimal("0.001")),
    "high": ("HIGH_PRICE", Decimal(1), Decimal("0.01")),
    "low": ("LOW_PRICE", Decimal(1), Decimal("0.01")),
    "volume": ("TRADE_VOL", Decimal(10000), Decimal("0.01")),
    "amount": ("TRADE_AMT", Decimal(10000), Decimal("0.01")),
}


def parameters(code, day):
    disclosure_parameters(code, day, day, 1)
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("Expected a canonical ISO daily statistics date")
    return {"sqlId": SQL_ID, "SEC_CODE": code, "TX_DATE": day,
            "TX_DATE_MON": "", "TX_DATE_YEAR": ""}


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Non-numeric source observation") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Non-finite or negative source observation")
    return result


def validate(payload, code, day):
    if (not isinstance(payload, dict) or payload.get("sqlId") != SQL_ID
            or payload.get("actionErrors") or payload.get("fieldErrors")
            or not isinstance(payload.get("result"), list)):
        raise ValueError("SSE daily statistics query returned an error or different recipe")
    rows = payload["result"]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Expected one daily statistics row, not monthly/yearly aggregates")
    row = rows[0]
    if (not isinstance(row, dict) or row.get("SEC_CODE") != code
            or row.get("TX_DATE") != day.replace("-", "") or row.get("DATE_TYPE") != "D"):
        raise ValueError("SSE statistics security/date/aggregation mismatch")
    for field, _, _ in FIELDS.values():
        number(row.get(field))
    return row


def compare(raw, official):
    result = {}
    for field, (key, multiplier, resolution) in FIELDS.items():
        observed, source = number(raw[field]), number(official[key])
        center, tolerance = source * multiplier, resolution * multiplier / 2
        difference = observed - center
        result[field] = {
            "provider_raw": str(raw[field]), "sse_raw": str(official[key]),
            "multiplier": str(multiplier), "reported_resolution": str(resolution),
            "difference_base_units": str(difference),
            "rounding_interval_half_width": str(tolerance),
            "exact_match": difference == 0,
            "within_rounding_interval": abs(difference) <= tolerance,
        }
    return result


def load_panel(view):
    view = Path(view)
    result = json.loads((view / "result.json").read_text(encoding="utf-8"))
    if result.get("status") != "source_cache_complete":
        raise ValueError("Expected a completed frozen raw fund panel")
    raw = view / "raw_daily.parquet"
    digest = sha256(raw.read_bytes()).hexdigest()
    if digest != result.get("raw_daily_sha256"):
        raise ValueError("Frozen raw fund panel hash changed")
    frame = pd.read_parquet(raw)
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("Frozen panel has duplicate security/date observations")
    return frame, digest


def collect(codes, days, view, folder=None, session=None):
    codes, days = list(codes), list(days)
    if (not codes or not days or len(codes) * len(days) > 32
            or len(set(codes)) != len(codes) or len(set(days)) != len(days)):
        raise ValueError("Use at most 32 distinct fund/day pairs per spot-check")
    for code in codes:
        for day in days:
            parameters(code, day)
    frame, digest = load_panel(view)
    pairs = [(code, day) for code in codes for day in days]
    indexed = frame.set_index(["code", "date"])
    if any(("sh." + code, day) not in indexed.index for code, day in pairs):
        raise ValueError("Requested spot-check is absent from the frozen panel")
    folder = Path(folder) if folder else DATA_ROOT / "research/fund_statistics" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    state = {"status": "collecting", "view": str(Path(view).resolve()),
             "raw_daily_sha256": digest, "recipe_url": RECIPE,
             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
             "requested_pairs": pairs, "receipts": [], "comparisons": [],
             "execution_admitted": False, "strategy_validated": False,
             "limits": ["SSE fund/day spot sample only, not a full-series unit reconciliation",
                        "Volume in 10000 fund units and amount in 10000 RMB, rounded to two decimals",
                        "High/low cent rounding and open/close mill-resolution intervals are diagnostic assumptions",
                        "Rounded statistics must not replace original quotes or establish exact reference prices",
                        "No suspension, rule, adjustment, distribution or account admission"]}
    write_json(folder / "result.json", state)
    owned_session = session is None
    session = requests.Session() if owned_session else session
    try:
        for code, day in pairs:
            params = parameters(code, day)
            raw = fetch(session, params)
            relative = f"{code}_{day}.json"
            (folder / relative).write_bytes(raw)
            state["receipts"].append({"file": relative, "params": params,
                                      "sha256": sha256(raw).hexdigest(),
                                      "retrieved_at": datetime.now().astimezone().isoformat()})
            write_json(folder / "result.json", state)
            row = validate(json.loads(raw), code, day)
            comparison = {"code": code, "date": day, "source_status": "missing" if row is None else "observed"}
            if row is not None:
                comparison["fields"] = compare(indexed.loc[("sh." + code, day)].to_dict(), row)
            state["comparisons"].append(comparison)
            write_json(folder / "result.json", state)
            print(f"{code} {day}: {comparison['source_status']}", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="stopped_source_error", failed_pair=[code, day],
                     error=f"{type(error).__name__}: {error}")
        write_json(folder / "result.json", state)
        raise
    finally:
        if owned_session:
            session.close()
    observed = [item for item in state["comparisons"] if item["source_status"] == "observed"]
    conflicts = [{"code": item["code"], "date": item["date"], "field": field, **values}
                 for item in observed for field, values in item["fields"].items()
                 if not values["within_rounding_interval"]]
    state.update(status="completed_public_statistics_spot_check", observed_pairs=len(observed),
                 missing_pairs=len(pairs) - len(observed), conflicts=conflicts)
    write_json(folder / "result.json", state)
    return folder, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--dates", required=True)
    parser.add_argument("--view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = collect(args.codes.split(","), args.dates.split(","), args.view, args.out)
    print(json.dumps({"folder": str(folder), "status": result["status"],
                      "observed_pairs": result["observed_pairs"], "missing_pairs": result["missing_pairs"],
                      "conflicts": len(result["conflicts"]), "execution_admitted": False}), flush=True)
    return 0
