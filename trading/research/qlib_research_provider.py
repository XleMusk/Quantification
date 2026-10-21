"""Export the full canonical research view to Qlib without admitting a trading feed."""
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
from .factors import catalog, features_from_provider, normalized_prices, write_provider


def build(price_view, destination=None, *, smoke_codes=()):
    price_view = Path(price_view)
    folder = Path(destination) if destination else DATA_ROOT / "research/qlib_research_provider" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for path, name in ((Path(__file__), "implementation.py"),
                       (Path(__file__).with_name("factors.py"), "normalization_implementation.py"),
                       (price_view / "result.json", "input_prices.json")):
        shutil.copy2(path, folder / name)
    result = {"status": "building", "price_view": str(price_view),
              "scope": "all_canonical_provider_observations_not_strategy_selection_or_PIT_membership",
              "execution_admitted": False, "strategy_validated": False, "model_trained": False,
              "started_at": datetime.now().astimezone().isoformat(),
              "implementation_sha256": sha256((folder / "implementation.py").read_bytes()).hexdigest(),
              "normalization_implementation_sha256": sha256((folder / "normalization_implementation.py").read_bytes()).hexdigest(),
              "limitations": ["Research price derivations retained, not independently verified source prices or cash/share accounting",
                              "Inactive observations masked; missing calendar observations stay NaN, not zero returns",
                              "Binary format and native-feature smoke are not a full factor scan, training or ledger backtest",
                              "No PIT membership, unused holdout claim, account/default dataset change or trading permission"]}
    write_json(folder / "result.json", result)
    start = time.monotonic()
    try:
        prices = json.loads((price_view / "result.json").read_text(encoding="utf-8"))
        if (prices["status"] != "research_reconstruction_completed" or not prices.get("derived_research_levels")
                or prices["execution_admitted"] is not False):
            raise ValueError("Completed explicit canonical research prices required")
        canonical_path = checked_file(price_view, prices["canonical_panel_file"], prices["canonical_panel_sha256"])
        raw_view = Path(prices["raw_view"])
        raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
        raw_path = checked_file(raw_view, "raw_daily.parquet", prices["source_hashes"]["raw"])
        raw_plan = json.loads(checked_file(raw_view, "plan.json", raw_result["plan_sha256"]).read_text(encoding="utf-8"))
        calendar_path = checked_file(raw_view, "input_calendar.csv", raw_plan["calendar_sha256"])
        calendar = pd.read_csv(calendar_path, dtype=str).date.tolist()
        fields = ["open", "high", "low", "close"]
        raw = pd.read_parquet(raw_path, columns=["code", "date", *fields, "volume", "amount", "tradestatus"])
        raw = raw.loc[~raw.code.eq(prices["rename"]["old"])].copy()
        for field in [*fields, "volume", "amount", "tradestatus"]:
            raw[field] = pd.to_numeric(raw[field], errors="raise")
        adjusted = pd.read_parquet(canonical_path, columns=["code", "date", *fields, "adjustflag"])
        for field in fields:
            adjusted[field] = pd.to_numeric(adjusted[field], errors="raise")
        codes = sorted(raw.code.unique())
        if (len(raw) != prices["canonical_rows"] or len(codes) != prices["canonical_symbols"]
                or sorted(raw.date.unique()) != calendar or not set(smoke_codes) <= set(codes)):
            raise ValueError("Full canonical observations/calendar changed or unknown smoke code")
        symbols = {code: normalize_symbol(code).vt_symbol for code in codes}
        for frame in (raw, adjusted):
            frame["symbol"] = frame.code.map(symbols)
            frame.drop(columns="code", inplace=True)
            frame["date"] = pd.to_datetime(frame.date)
            frame.set_index(["date", "symbol"], inplace=True)
            frame.sort_index(inplace=True)
        panel, quality = normalized_prices(raw, adjusted)
        del raw, adjusted
        panel = panel.reset_index()
        provider = folder / "qlib_provider"
        provider_symbols = write_provider(panel, calendar, provider)
        definitions = catalog()
        definitions.to_csv(folder / "alpha158_catalog.csv", index=False)
        result.update(provider_folder=str(provider), rows=len(panel), symbols=len(provider_symbols), dates=len(calendar),
                      rename=prices["rename"],
                      fields=["open", "high", "low", "close", "volume", "vwap", "factor"],
                      binary_files=len(provider_symbols) * 7, normalization=quality,
                      canonical_panel_sha256=prices["canonical_panel_sha256"], raw_daily_sha256=prices["source_hashes"]["raw"],
                      alpha158_definitions=len(definitions))
        if smoke_codes:
            smoke = features_from_provider(provider, [symbols[code] for code in smoke_codes], calendar, definitions, [])
            smoke.to_parquet(folder / "native_alpha158_smoke.parquet")
            result["native_smoke"] = {"codes": list(smoke_codes), "rows": len(smoke), "features": len(smoke.columns),
                                      "finite_feature_cells": int(smoke.notna().sum().sum()),
                                      "purpose": "native expression consumer check, not factor effectiveness or model training"}
        result["status"] = "research_provider_completed"
    except Exception as error:
        result.update(status="failed_provider", error=f"{type(error).__name__}: {error}")
    result.update(finished_at=datetime.now().astimezone().isoformat(), elapsed_seconds=round(time.monotonic() - start, 3))
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--smoke-codes", default="", help="Comma-separated source codes for native Alpha158 ingestion checks only")
    args = parser.parse_args(argv)
    codes = tuple(code.strip() for code in args.smoke_codes.split(",") if code.strip())
    if len(set(codes)) != len(codes):
        parser.error("Smoke codes must be distinct")
    folder, result = build(args.price_view, args.out, smoke_codes=codes)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0 if result["status"] == "research_provider_completed" else 1
