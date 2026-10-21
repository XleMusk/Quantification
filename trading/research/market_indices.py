"""Dated CSI index inputs and native-Qlib MASTER market features, not tradable assets."""

import argparse
import ast
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import socket
import uuid

import numpy as np
import pandas as pd

from ..market_dataset import DEFAULT_ROOT, baostock_session, provider_frame, write_frame, write_json
from ..settings import DATA_ROOT
from .factors import catalog, native_features


INDICES = ("sh.000300", "sh.000905", "sh.000906")
WINDOWS = (5, 10, 20, 30, 60)
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
SOURCE_COMMIT = "de8f58557096abde4216a701b35fc4368158d111"
RECIPE_HASH = "a958de1bb78303747ed739c95d2f1a13242a235a735d7273de6f88b0f21d9150"
RECIPE_URL = f"https://github.com/SJTU-DMTai/MASTER/blob/{SOURCE_COMMIT}/README.md"
RECIPE_FILE = DATA_ROOT / "research/primary_sources/MASTER" / SOURCE_COMMIT / "README.md"


def index_symbol(code):
    if code not in INDICES:
        raise ValueError("Only the three declared CSI indices are supported")
    return code[3:] + ".SSE"


def single_catalog():
    ret = "$close/Ref($close,1)-1"
    rows = [("return_1d", ret)]
    for window in WINDOWS:
        rows += [(f"return_mean_{window}", f"Mean({ret},{window})"),
                 (f"return_std_{window}", f"Std({ret},{window})"),
                 (f"amount_mean_ratio_{window}", f"Mean($amount,{window})/$amount"),
                 (f"amount_std_ratio_{window}", f"Std($amount,{window})/$amount")]
    return pd.DataFrame(rows, columns=["name", "formula"])


def feature_names():
    return [f"MKT_{code.replace('.', '').upper()}_{name}" for code in INDICES for name in single_catalog().name]


def source_formulas(text):
    for block in re.findall(r"```python\s*\n(.*?)\n```", text, re.DOTALL):
        if not block.lstrip().startswith("['Mask("):
            continue
        node = ast.parse(block).body[0].value
        # The README contains a second list with a typo; use the first complete list.
        if isinstance(node, ast.Tuple) and len(node.elts) == 1:
            node = node.elts[0]
        values = ast.literal_eval(node)
        expected = [f'Mask({formula}, "{code.replace(".", "")}")'
                    for code in INDICES for formula in single_catalog().formula]
        if values != expected:
            raise ValueError("Pinned MASTER market formulas/order do not match")
        return values
    raise ValueError("No explicit MASTER market formula list")


def create(source_root=DEFAULT_ROOT, out=None, recipe_file=RECIPE_FILE):
    source_root, recipe_file = Path(source_root), Path(recipe_file)
    recipe = recipe_file.read_bytes()
    if sha256(recipe).hexdigest() != RECIPE_HASH:
        raise ValueError("MASTER source README differs from the reviewed commit")
    formulas = source_formulas(recipe.decode("utf-8"))
    plan = json.loads((source_root / "collection_plan.json").read_text(encoding="utf-8"))
    if plan["source"] != "BaoStock":
        raise ValueError("Expected the existing BaoStock source calendar/master")
    master = pd.read_csv(source_root / "provider_instruments.csv", dtype=str, keep_default_na=False)
    selected = master.loc[master.code.isin(INDICES)].copy()
    if (len(selected) != 3 or selected.code.duplicated().any() or not selected.type.eq("2").all()
            or any(not row.ipoDate or row.ipoDate > plan["start"] or row.outDate
                   for row in selected.itertuples())):
        raise ValueError("Provider master does not identify all three established indices")
    selected = selected.set_index("code").loc[list(INDICES)].reset_index()
    calendar = pd.read_csv(source_root / "calendar.csv", dtype=str)
    days = calendar.date.tolist()
    if not days or days != sorted(set(days)) or any(not plan["start"] <= d <= plan["end"] for d in days):
        raise ValueError("Invalid source calendar")
    folder = Path(out) if out else DATA_ROOT / "research/market_indices" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    write_frame(folder / "instruments.csv", selected)
    write_frame(folder / "calendar.csv", calendar)
    shutil.copyfile(recipe_file, folder / "MASTER_README.md")
    manifest = {"status": "prepared", "source": "BaoStock", "start": plan["start"], "end": plan["end"],
                "fields": FIELDS, "indices": list(INDICES), "source_root": str(source_root.resolve()),
                "created_at": datetime.now().astimezone().isoformat(), "receipts": {},
                "calendar_sha256": sha256((folder / "calendar.csv").read_bytes()).hexdigest(),
                "master_sha256": sha256((folder / "instruments.csv").read_bytes()).hexdigest(),
                "recipe_source": {"url": RECIPE_URL, "commit": SOURCE_COMMIT, "sha256": RECIPE_HASH,
                                  "formulas": formulas, "names": feature_names()},
                "quote_unit": "index_points_not_CNY_order_price", "amount_unit": "provider_reported_RMB",
                "scope": "research_index_context_not_equity_or_ETF_execution_feed",
                "model_trained": False, "strategy_validated": False, "execution_admitted": False,
                "limitations": ["Provider-reported index history is not independently index-company reconciled",
                                "Index points must never enter security cash/share accounting",
                                "No scaling fit, eight-session tensor, training, score or account promotion",
                                "EOD historical feature availability is assumed; receipt/publication time is not historical PIT proof"]}
    write_json(folder / "manifest.json", manifest)
    return folder


def load(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["source"] != "BaoStock" or manifest["indices"] != list(INDICES)
            or manifest["fields"] != FIELDS or manifest["recipe_source"]["sha256"] != RECIPE_HASH):
        raise ValueError("Index collection specification changed")
    for name, key in (("calendar.csv", "calendar_sha256"), ("instruments.csv", "master_sha256")):
        if sha256((folder / name).read_bytes()).hexdigest() != manifest[key]:
            raise ValueError("Index calendar/master changed")
    recipe = (folder / "MASTER_README.md").read_bytes()
    if sha256(recipe).hexdigest() != RECIPE_HASH or source_formulas(recipe.decode("utf-8")) != manifest["recipe_source"]["formulas"]:
        raise ValueError("Index feature source changed")
    return manifest, pd.read_csv(folder / "calendar.csv", dtype=str).date.tolist()


def normalize(frame, code, calendar):
    if frame.empty or not set(FIELDS.split(",")) <= set(frame):
        raise ValueError("Missing declared index history fields")
    frame = frame.copy()
    if (not frame.code.eq(code).all() or frame.date.duplicated().any()
            or sorted(frame.date.tolist()) != calendar):
        raise ValueError("Index identity, duplicate or calendar gap")
    for field in FIELDS.split(",")[2:]:
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    numbers = frame.loc[:, FIELDS.split(",")[2:]]
    prices = frame[["open", "high", "low", "close", "preclose"]]
    if (not np.isfinite(numbers).all().all() or prices.le(0).any().any()
            or frame.amount.le(0).any() or frame.volume.lt(0).any()
            or frame.high.lt(prices[["open", "low", "close"]].max(axis=1)).any()
            or frame.low.gt(prices[["open", "high", "close"]].min(axis=1)).any()):
        raise ValueError("Invalid index points or amount values")
    frame["symbol"] = index_symbol(code)
    return frame.sort_values("date").reset_index(drop=True)


def collect(folder):
    folder = Path(folder)
    manifest, calendar = load(folder)
    pending = [code for code in INDICES if code not in manifest["receipts"]]
    socket.setdefaulttimeout(30)
    try:
        if pending:
            with baostock_session() as provider:
                for code in pending:
                    print(f"Collect index {code}", flush=True)
                    raw = provider_frame(provider.query_history_k_data_plus(
                        code, FIELDS, start_date=manifest["start"], end_date=manifest["end"], frequency="d", adjustflag="3"))
                    raw_path = folder / (code.replace(".", "") + "_response.csv")
                    write_frame(raw_path, raw)
                    frame = normalize(raw, code, calendar)
                    path = folder / (code.replace(".", "") + ".parquet")
                    write_frame(path, frame)
                    manifest["receipts"][code] = {"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(),
                                                "raw_file": raw_path.name, "raw_sha256": sha256(raw_path.read_bytes()).hexdigest(),
                                                "rows": len(frame), "retrieved_at": datetime.now().astimezone().isoformat()}
                    write_json(folder / "manifest.json", manifest)
        manifest.update(status="raw_complete", error=None)
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(folder / "manifest.json", manifest)
        raise
    write_json(folder / "manifest.json", manifest)
    return manifest


def extend(folder, calendar_source, out=None):
    """Append only missing raw index sessions; preserve the old feature archive."""
    folder, calendar_source = Path(folder), Path(calendar_source)
    previous, prefix = load(folder)
    days = pd.read_csv(calendar_source, dtype=str).date.tolist()
    if (previous.get("status") not in {"raw_complete", "features_ready"}
            or set(previous["receipts"]) != set(INDICES) or len(days) <= len(prefix)
            or days[:len(prefix)] != prefix or days != sorted(set(days))
            or any(datetime.strptime(d, "%Y-%m-%d").date().isoformat() != d for d in days)):
        raise ValueError("Raw extension requires a complete original index archive and an unchanged calendar prefix")
    missing = days[len(prefix):]
    out = Path(out) if out else DATA_ROOT / "research/market_indices" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    for name in ("instruments.csv", "MASTER_README.md"):
        shutil.copyfile(folder / name, out / name)
    write_frame(out / "calendar.csv", pd.DataFrame({"date": days}))
    manifest = {**previous, "status": "extending_raw", "end": days[-1], "receipts": {},
        "created_at": datetime.now().astimezone().isoformat(),
        "calendar_sha256": sha256((out / "calendar.csv").read_bytes()).hexdigest(),
        "extension": {"source_folder": str(folder.resolve()), "source_manifest_sha256": sha256((folder / "manifest.json").read_bytes()).hexdigest(),
            "calendar_source": str(calendar_source.resolve()), "calendar_source_sha256": sha256(calendar_source.read_bytes()).hexdigest(),
            "preserved_sessions": len(prefix), "new_sessions": missing,
            "acquisition": "only missing raw index dates; no native-Qlib feature rebuild"}}
    manifest.pop("features", None)
    manifest.pop("error", None)
    write_json(out / "manifest.json", manifest)
    socket.setdefaulttimeout(30)
    try:
        with baostock_session() as provider:
            for code in INDICES:
                receipt = previous["receipts"][code]
                for file_key, hash_key in (("file", "sha256"), ("raw_file", "raw_sha256")):
                    if sha256((folder / receipt[file_key]).read_bytes()).hexdigest() != receipt[hash_key]:
                        raise ValueError("Original raw index archive changed")
                old = normalize(pd.read_parquet(folder / receipt["file"]), code, prefix)
                print(f"Append {len(missing)} index sessions: {code}", flush=True)
                tail = provider_frame(provider.query_history_k_data_plus(code, FIELDS,
                    start_date=missing[0], end_date=missing[-1], frequency="d", adjustflag="3"))
                added = normalize(tail, code, missing)
                raw = pd.concat([pd.read_csv(folder / receipt["raw_file"], dtype=str), tail], ignore_index=True)
                frame = pd.concat([old, added], ignore_index=True)
                normalize(frame, code, days)
                path, raw_path = out / receipt["file"], out / receipt["raw_file"]
                write_frame(path, frame)
                write_frame(raw_path, raw)
                manifest["receipts"][code] = {"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(),
                    "raw_file": raw_path.name, "raw_sha256": sha256(raw_path.read_bytes()).hexdigest(),
                    "rows": len(frame), "appended_rows": len(added), "retrieved_at": datetime.now().astimezone().isoformat()}
                write_json(out / "manifest.json", manifest)
        manifest.update(status="raw_complete", error=None)
    except (Exception, KeyboardInterrupt) as error:
        manifest.update(status="failed_raw_extension", error=f"{type(error).__name__}: {error}")
        write_json(out / "manifest.json", manifest)
        raise
    write_json(out / "manifest.json", manifest)
    return out, manifest


def compute(frame, calendar, provider_folder):
    values = native_features(frame, calendar, provider_folder, single_catalog(), [], provider_fields=("close", "amount"))
    pieces = []
    for code in INDICES:
        piece = values.xs(index_symbol(code), level="symbol").reindex(pd.to_datetime(calendar))
        piece.columns = [f"MKT_{code.replace('.', '').upper()}_{name}" for name in single_catalog().name]
        pieces.append(piece)
    result = pd.concat(pieces, axis=1)
    result.index.name = "date"
    return result


def build(folder):
    folder = Path(folder)
    manifest, calendar = load(folder)
    panels = []
    for code in INDICES:
        receipt = manifest["receipts"].get(code)
        if receipt is None:
            raise ValueError("All three index histories are required")
        for name, key in (("file", "sha256"), ("raw_file", "raw_sha256")):
            if sha256((folder / receipt[name]).read_bytes()).hexdigest() != receipt[key]:
                raise ValueError("Index receipt file changed")
        panels.append(normalize(pd.read_parquet(folder / receipt["file"]), code, calendar))
    if manifest.get("features"):
        entry = manifest["features"]
        path = folder / entry["file"]
        if sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Saved index feature matrix changed")
        return pd.read_parquet(path).set_index("date")
    try:
        panel = pd.concat(panels, ignore_index=True)
        panel["date"] = pd.to_datetime(panel.date)
        result = compute(panel, calendar, folder / "qlib_provider")
        path = folder / "market_features.parquet"
        write_frame(path, result.reset_index())
        manifest.update(status="features_ready", error=None,
                        features={"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(),
                                  "rows": len(result), "columns": len(result.columns), "null_cells": int(result.isna().sum().sum()),
                                  "normalization": "raw native Qlib expressions, no fitted processor or fill",
                                  "first_return": "unknown before source start, left NaN", "std_ddof": 1, "rolling_min_periods": 1})
    except Exception as error:
        manifest.update(status="features_failed", error=f"{type(error).__name__}: {error}")
        write_json(folder / "manifest.json", manifest)
        raise
    write_json(folder / "manifest.json", manifest)
    return result


def join_stock_features(stocks, market):
    if (not isinstance(stocks.index, pd.MultiIndex) or stocks.index.names != ["date", "symbol"]
            or stocks.index.has_duplicates or stocks.empty or stocks.columns.tolist() != catalog().name.tolist()):
        raise ValueError("Expected dated, unique native Alpha158 rows in original order")
    if market.index.has_duplicates or market.columns.tolist() != feature_names():
        raise ValueError("Expected dated 63-column market features in declared order")
    dates = pd.to_datetime(stocks.index.get_level_values("date"))
    canonical = pd.MultiIndex.from_arrays([dates, stocks.index.get_level_values("symbol")], names=["date", "symbol"])
    if canonical.has_duplicates or not dates.equals(dates.normalize()):
        raise ValueError("Stock feature dates must identify unique daily observations")
    selected = market.reindex(dates)
    if selected.isna().any().any() or not np.isfinite(selected.to_numpy()).all():
        raise ValueError("Market inputs missing or still in warmup; do not silently drop stock rows")
    joined = stocks.copy()
    joined.index = canonical
    return joined.join(market, on="date", validate="many_to_one")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out", type=Path, help="New research directory; never overwrites an existing one")
    parser.add_argument("--folder", type=Path, help="Consume or explicitly resume saved index inputs")
    parser.add_argument("--extend-from", type=Path, help="Append raw sessions to a new archive, leaving original inputs/features unchanged")
    parser.add_argument("--calendar-source", type=Path, help="Actual covered exchange calendar for --extend-from")
    parser.add_argument("--raw-only", action="store_true", help="Do not build native Qlib market features")
    parser.add_argument("--stock-features", type=Path, help="Optional existing native Alpha158 parquet; no labels or retraining")
    args = parser.parse_args(argv)
    if args.extend_from:
        if args.folder or not args.calendar_source or not args.raw_only or args.stock_features:
            parser.error("Raw extension requires --calendar-source --raw-only and no --folder/--stock-features")
        folder, manifest = extend(args.extend_from, args.calendar_source, args.out)
        print(json.dumps({"folder": str(folder), "status": manifest["status"],
                          "appended_sessions_per_index": len(manifest["extension"]["new_sessions"]),
                          "features_rebuilt": False, "execution_admitted": False}), flush=True)
        return 0
    if args.calendar_source:
        parser.error("--calendar-source requires --extend-from")
    if args.folder and args.out:
        parser.error("Choose an existing folder or a new output, not both")
    folder = args.folder or create(args.source_root, args.out)
    manifest, _ = load(folder)
    if len(manifest["receipts"]) < 3:
        collect(folder)
    if args.raw_only:
        if args.stock_features:
            parser.error("--raw-only cannot join native stock features")
        print(json.dumps({"folder": str(folder), "status": "raw_complete", "features_rebuilt": False,
                          "execution_admitted": False}), flush=True)
        return 0
    market = build(folder)
    if args.stock_features:
        stocks = pd.read_parquet(args.stock_features)
        joined = join_stock_features(stocks, market)
        destination = folder / "stock_inputs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
        destination.mkdir(parents=True, exist_ok=False)
        path = destination / "features_221.parquet"
        joined.to_parquet(path)
        write_json(destination / "receipt.json", {"source_features": str(args.stock_features.resolve()),
                   "source_sha256": sha256(args.stock_features.read_bytes()).hexdigest(), "index_inputs": str(folder.resolve()),
                   "file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(),
                   "rows": len(joined), "columns": len(joined.columns), "stocks": int(joined.index.get_level_values("symbol").nunique()),
                   "dates": int(joined.index.get_level_values("date").nunique()), "row_filter": "none, all supplied eligible rows preserved",
                   "labels_used": False, "scaled": False, "sequence_tensor_built": False,
                   "model_trained": False, "execution_admitted": False})
        print(f"Joined raw input: {destination}", flush=True)
    print(json.dumps({"folder": str(folder), "market_rows": len(market), "market_columns": len(market.columns),
                      "model_trained": False, "execution_admitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
