"""Read cached ETF quotes through the precise Price contract; never admit a feed."""

import argparse
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from ..contracts import Price, PriceBasis
from ..rules import cents
from ..settings import DATA_ROOT
from .experiment import write_json
from .full_market_etf import SOURCES


def inspect(frame, code):
    if frame.empty or not {"date", "symbol", "open", "high", "low", "close"} <= set(frame):
        raise ValueError("Missing cached ETF quote fields")
    if not frame.symbol.eq(code).all() or frame.date.duplicated().any():
        raise ValueError("Cached ETF identity/date mismatch")
    cells, subcent, invalid, max_error, examples = 0, 0, 0, Decimal(0), []
    for field in ("open", "high", "low", "close"):
        for day, value in zip(frame.date, frame[field]):
            cells += 1
            try:
                price = Price.from_rmb(value, PriceBasis.RAW, "mill")
                # The old cent quote representation must not be applied to these inputs.
                rounded = Decimal(cents(value)) / 100
                error = abs(price.rmb - rounded)
                subcent += int(error != 0)
                max_error = max(max_error, error)
            except (ValueError, ArithmeticError) as error:
                invalid += 1
                if len(examples) < 5:
                    examples.append({"date": str(day), "field": field, "value": str(value), "error": str(error)})
    return {"code": code, "rows": len(frame), "price_cells": cells, "subcent_cells": subcent,
            "invalid_mill_tick_cells": invalid, "max_cent_rounding_error_rmb": str(max_error),
            "invalid_examples": examples}


def run(folder):
    folder = Path(folder)
    state = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    if state.get("provider") != "sina-ohlcv" or state.get("source") != SOURCES["sina-ohlcv"]:
        raise ValueError("Expected saved, research-only Sina ETF receipts")
    destination = DATA_ROOT / "research/etf_quote_precision" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "input": str(folder.resolve()), "input_status_sha256": sha256(
        (folder / "status.json").read_bytes()).hexdigest(), "execution_admitted": False, "strategy_validated": False,
        "limitations": ["Price tick/round-trip checks are not adjustment, volume, status, membership or rule validation",
                        "Only existing successful files are read; failed receipts are not retried or silently replaced",
                        "No targets, orders, fills, broker action or ETF performance is generated"]}
    write_json(destination / "result.json", result)
    entries, unavailable = [], []
    try:
        for code, receipt in sorted(state["receipts"].items()):
            if receipt["status"] != "acquired_unvalidated":
                unavailable.append({"code": code, "status": receipt["status"]})
                continue
            if not re.fullmatch(r"[0-9]{6}", code) or receipt["file"] != code + ".parquet":
                raise ValueError("Unexpected cached ETF path/identity")
            path = folder / receipt["file"]
            if sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
                raise ValueError("Cached ETF quote receipt changed: " + code)
            frame = pd.read_parquet(path)
            if len(frame) != receipt["rows"]:
                raise ValueError("Cached ETF row count changed")
            entries.append({**inspect(frame, code), "sha256": receipt["sha256"]})
        pd.DataFrame(entries).to_csv(destination / "by_code.csv", index=False)
        write_json(destination / "unavailable_receipts.json", unavailable)
        result.update(status="completed_quote_precision_diagnostic", codes=len(entries),
                      rows=sum(row["rows"] for row in entries), price_cells=sum(row["price_cells"] for row in entries),
                      subcent_cells=sum(row["subcent_cells"] for row in entries),
                      invalid_mill_tick_cells=sum(row["invalid_mill_tick_cells"] for row in entries),
                      unavailable_codes=len(unavailable),
                      max_cent_rounding_error_rmb=str(max(Decimal(row["max_cent_rounding_error_rmb"]) for row in entries)))
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(destination / "result.json", result)
        raise
    write_json(destination / "result.json", result)
    return destination, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args(argv)
    folder, result = run(args.folder)
    print(json.dumps({"folder": str(folder), **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
