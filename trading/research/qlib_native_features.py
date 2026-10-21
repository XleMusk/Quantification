"""Compute the pinned native Alpha158 over every provider code, without fitting/selecting."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time
import uuid

import pandas as pd

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from ..symbols import normalize_symbol
from .adjusted_prices import checked_file
from .factors import features_from_provider, qcode


def build(provider_view, destination=None, *, batch_size=256):
    if not 1 <= batch_size <= 512:
        raise ValueError("Batch size must be 1..512; it controls memory, not strategy scope")
    provider_view = Path(provider_view)
    folder = Path(destination) if destination else DATA_ROOT / "research/qlib_native_features" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for path, name in ((Path(__file__), "implementation.py"),
                       (Path(__file__).with_name("factors.py"), "native_implementation.py"),
                       (provider_view / "result.json", "input_provider.json")):
        shutil.copy2(path, folder / name)
    result = {"status": "building", "provider_view": str(provider_view), "batch_size": batch_size,
              "scope": "every_pinned_provider_code_complete_calendar_grid_not_strategy_selection",
              "execution_admitted": False, "strategy_validated": False, "model_trained": False,
              "factor_selection_performed": False, "symbols": 0, "rows": 0, "partitions": [],
              "started_at": datetime.now().astimezone().isoformat(),
              "implementation_sha256": sha256((folder / "implementation.py").read_bytes()).hexdigest(),
              "native_implementation_sha256": sha256((folder / "native_implementation.py").read_bytes()).hexdigest(),
              "limitations": ["Calendar grid includes unobserved/pre-listing/retired dates; not all rows are tradable observations",
                              "Dated eligibility, company actions and same-ledger comparison remain separate requirements",
                              "Research derivations and non-PIT source coverage retained; no independent source or untouched-test claim",
                              "Native features are not factor effectiveness, fitted forecasts, account returns or permission to trade"]}
    write_json(folder / "result.json", result)
    start = time.monotonic()
    try:
        provider_result = json.loads((provider_view / "result.json").read_text(encoding="utf-8"))
        if (provider_result["status"] != "research_provider_completed"
                or provider_result["execution_admitted"] is not False
                or provider_result["model_trained"] is not False):
            raise ValueError("Completed untrained research provider required")
        provider = Path(provider_result["provider_folder"])
        provider.resolve().relative_to(provider_view.resolve())
        calendar_path, instrument_path = provider / "calendars/day.txt", provider / "instruments/all.txt"
        calendar = calendar_path.read_text(encoding="utf-8").splitlines()
        instruments = [line.split("\t") for line in instrument_path.read_text(encoding="utf-8").splitlines()]
        symbols = [normalize_symbol(row[0]).vt_symbol for row in instruments]
        if (not calendar or calendar != sorted(set(calendar)) or len(calendar) != provider_result["dates"]
                or len(symbols) != provider_result["symbols"] or symbols != sorted(set(symbols))
                or any(len(row) != 3 or row[1:] != [calendar[0], calendar[-1]] for row in instruments)):
            raise ValueError("Full provider instrument/calendar identity changed")
        definitions = pd.read_csv(provider_view / "alpha158_catalog.csv", keep_default_na=False)
        if len(definitions) != 158 or definitions.name.duplicated().any() or definitions.formula.eq("").any():
            raise ValueError("Pinned complete native Alpha158 definitions required")
        shutil.copy2(provider_view / "alpha158_catalog.csv", folder / "alpha158_catalog.csv")
        fields = provider_result["fields"]
        if fields != ["open", "high", "low", "close", "volume", "vwap", "factor"]:
            raise ValueError("Require the shared seven-field normalized provider")
        inventory = []
        for symbol in symbols:
            for field in fields:
                path = provider / "features" / qcode(symbol).lower() / (field + ".day.bin")
                payload = path.read_bytes()
                if len(payload) != (len(calendar) + 1) * 4:
                    raise ValueError("Provider binary calendar length changed")
                inventory.append({"file": path.relative_to(provider).as_posix(), "sha256": sha256(payload).hexdigest()})
        write_json(folder / "input_binary_hashes.json", inventory)
        result.update(expected_symbols=len(symbols), expected_rows=len(symbols) * len(calendar), dates=len(calendar), features=158,
                      provider_calendar_sha256=sha256(calendar_path.read_bytes()).hexdigest(),
                      provider_instruments_sha256=sha256(instrument_path.read_bytes()).hexdigest(),
                      binary_files=len(inventory), input_binary_hashes_sha256=sha256((folder / "input_binary_hashes.json").read_bytes()).hexdigest(),
                      catalog_sha256=sha256((folder / "alpha158_catalog.csv").read_bytes()).hexdigest())
        import qlib
        result["qlib_version"] = qlib.__version__
        for offset in range(0, len(symbols), batch_size):
            batch = symbols[offset:offset + batch_size]
            frame = features_from_provider(provider, batch, calendar, definitions, [])
            wanted = pd.MultiIndex.from_product([pd.to_datetime(calendar), batch], names=["date", "symbol"])
            if (not frame.index.equals(wanted.sort_values()) or frame.columns.tolist() != definitions.name.tolist()):
                raise ValueError("Native features omitted/relabelled code/date/definition identities")
            name = f"features_{offset // batch_size:04d}.parquet"
            temporary = folder / (name + ".partial")
            frame.to_parquet(temporary)
            temporary.replace(folder / name)
            finite = int(frame.notna().sum().sum())
            result["partitions"].append({"file": name, "symbols": batch, "rows": len(frame),
                                         "finite_feature_cells": finite, "sha256": sha256((folder / name).read_bytes()).hexdigest()})
            result["rows"] += len(frame)
            result["symbols"] += len(batch)
            write_json(folder / "result.json", result)
            print(f"{result['symbols']}/{len(symbols)} codes; {result['rows']} calendar-grid feature rows", flush=True)
            del frame
        if result["rows"] != result["expected_rows"] or result["symbols"] != result["expected_symbols"]:
            raise ValueError("Native full-scope feature counts changed")
        result.update(status="full_native_features_completed",
                      finite_feature_cells=sum(part["finite_feature_cells"] for part in result["partitions"]))
    except Exception as error:
        result.update(status="failed_native_features", error=f"{type(error).__name__}: {error}")
    result.update(finished_at=datetime.now().astimezone().isoformat(), elapsed_seconds=round(time.monotonic() - start, 3))
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args(argv)
    folder, result = build(args.provider_view, args.out, batch_size=args.batch_size)
    brief = {key: value for key, value in result.items() if key != "partitions"}
    print(json.dumps({"folder": str(folder), **brief}, indent=2))
    return 0 if result["status"] == "full_native_features_completed" else 1
