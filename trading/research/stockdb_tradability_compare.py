"""Compare StockDB tradability proxies with an independent BaoStock raw view."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from hashlib import sha256
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .full_market import verify_chunks
from .full_market_mix import load_rows


def _code(value: object) -> str:
    text = str(value)
    return text.split(".")[-1].zfill(6)


def _read_raw_view(raw_view: Path, day: str) -> tuple[pd.DataFrame, Path]:
    """Read a pinned raw parquet view or one daily-source-probe CSV."""
    raw_view = Path(raw_view).resolve()
    if raw_view.is_file():
        raw_path = raw_view
    else:
        parquet = raw_view / "raw_daily.parquet"
        if parquet.is_file():
            raw_path = parquet
        else:
            candidates = sorted(raw_view.glob(f"{day}_stock.csv"))
            if len(candidates) != 1:
                raise FileNotFoundError(f"Pinned raw view has no unique {day}_stock.csv or raw_daily.parquet: {raw_view}")
            raw_path = candidates[0]
    columns = ["code", "date", "tradestatus", "isST"]
    if raw_path.suffix.lower() == ".csv":
        raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False, usecols=columns)
    else:
        raw = pd.read_parquet(raw_path, columns=columns)
    return raw, raw_path


def compare(snapshot: Path, raw_view: Path, date: str, output: Path | None = None) -> tuple[Path, dict]:
    snapshot = Path(snapshot).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_chunks(snapshot, manifest)
    if manifest.get("execution_admitted") is not False:
        raise ValueError("StockDB snapshot must remain unadmitted")
    day = pd.Timestamp(date).strftime("%Y-%m-%d")
    stockdb = load_rows(snapshot, manifest, {"0", "3", "6"})
    stockdb = stockdb.loc[stockdb.date.astype(str).eq(pd.Timestamp(date).strftime("%Y%m%d"))].copy()
    stockdb["code_key"] = stockdb.code.map(_code)
    stockdb["stockdb_proxy"] = stockdb[["open", "high", "low", "close", "volume", "amount"]].gt(0).all(axis=1)
    raw_view = Path(raw_view).resolve()
    raw, raw_path = _read_raw_view(raw_view, day)
    raw = raw.loc[raw.date.astype(str).eq(day)].copy()
    raw["code_key"] = raw.code.map(_code)
    if raw.duplicated(["code_key", "date"]).any():
        raise ValueError("Independent raw view has duplicate code/date rows")
    raw["baostock_tradable"] = pd.to_numeric(raw.tradestatus, errors="coerce").eq(1)
    raw["baostock_is_st"] = pd.to_numeric(raw.isST, errors="coerce")
    merged = stockdb[["code_key", "stockdb_proxy", "is_st"]].merge(
        raw[["code_key", "baostock_tradable", "baostock_is_st"]], on="code_key", how="inner")
    if merged.empty:
        raise ValueError("No paired rows for the requested comparison date")
    tradable_equal = merged.stockdb_proxy.eq(merged.baostock_tradable)
    st_equal = merged.is_st.astype(bool).eq(merged.baostock_is_st.astype(bool))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_tradability_compare" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_tradability_compare" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    pair = {
        "schema": "stockdb_baostock_tradability_compare_v1",
        "status": "completed_research_only_tradability_compare",
        "date": day,
        "snapshot": str(snapshot),
        "snapshot_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "raw_view": str(raw_view),
        "raw_file": str(raw_path),
        "stockdb_rows": int(len(stockdb)),
        "raw_rows": int(len(raw)),
        "paired_rows": int(len(merged)),
        "stockdb_only_rows": int(len(stockdb) - len(merged)),
        "raw_only_rows": int(len(raw) - len(merged)),
        "stockdb_proxy_true": int(stockdb.stockdb_proxy.sum()),
        "raw_tradestatus_true": int(raw.baostock_tradable.sum()),
        "tradestatus_equal_rows": int(tradable_equal.sum()),
        "tradestatus_disagreement_rows": int((~tradable_equal).sum()),
        "is_st_equal_rows": int(st_equal.sum()),
        "is_st_disagreement_rows": int((~st_equal).sum()),
        "execution_admitted": False,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "limitations": [
            "This is an overlap-date source comparison, not a current-day or PIT qualification proof.",
            "BaoStock raw tradestatus is used only as an independent diagnostic comparator; no fields are substituted into StockDB.",
            "No ranking, target holdings, paper orders, Ledger or THS state was generated.",
        ],
    }
    merged.to_csv(artifacts / "paired_rows.csv", index=False)
    (out / "receipt.json").write_text(json.dumps(pair, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB/BaoStock tradability overlap comparison", "",
        "Research-only overlap diagnostic; no field substitution or trading admission.",
        f"- Date: `{day}`; StockDB rows `{len(stockdb)}`; BaoStock rows `{len(raw)}`; paired `{len(merged)}`.",
        f"- Tradability proxy matches: `{int(tradable_equal.sum())}/{len(merged)}`; disagreements `{int((~tradable_equal).sum())}`.",
        f"- isST matches: `{int(st_equal.sum())}/{len(merged)}`; disagreements `{int((~st_equal).sum())}`.",
        f"- Artifacts: `{artifacts}`.",
    ]) + "\n", encoding="utf-8")
    return out / "receipt.json", pair


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--raw-view", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = compare(args.snapshot, args.raw_view, args.date, args.out)
    print(json.dumps({"receipt": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
