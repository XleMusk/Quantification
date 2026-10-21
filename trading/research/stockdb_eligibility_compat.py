"""Build a research-only StockDB eligibility compatibility receipt.

The historical ``dated_eligibility`` consumer requires four families of
inputs: an observed daily row with a tradability flag, an ``isST`` flag, an
IPO/out date, and a 20-session median amount.  StockDB currently exposes the
OHLCV/amount and ``is_st`` parts, but not ``tradestatus`` or a PIT listing
master.  This module measures that gap on one immutable StockDB snapshot.

It deliberately does not emit scores, rankings, target holdings, orders, or
paper plans.  The per-session and latest-day tables are diagnostics only.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from hashlib import sha256
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .full_market import verify_chunks
from ..settings import DATA_ROOT, ROOT


DEFAULT_MIN_AMOUNT = 20_000_000.0
DEFAULT_MIN_LISTING_DAYS = 365
DEFAULT_SELECTION_DATE = "2023-01-01"
REQUIRED_FIELDS = ("code", "date", "open", "high", "low", "close", "volume", "amount", "is_st")


def _exchange_code(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("0", "3")):
        return f"sz.{code}"
    raise ValueError(f"Unsupported StockDB equity code: {code}")


def _load_rows(snapshot: Path, manifest: dict) -> pd.DataFrame:
    """Load only fields needed for compatibility, preserving source rows."""

    frames = []
    for item in manifest["chunks"]:
        path = (snapshot / item["parquet_file"]).resolve()
        path.relative_to(snapshot.resolve())
        frame = pd.read_parquet(path, columns=list(REQUIRED_FIELDS))
        if frame.empty:
            continue
        missing = set(REQUIRED_FIELDS) - set(frame.columns)
        if missing:
            raise ValueError(f"StockDB chunk is missing fields: {sorted(missing)}")
        frames.append(frame)
    if not frames:
        raise ValueError("StockDB snapshot contains no eligibility rows")
    rows = pd.concat(frames, ignore_index=True)
    rows["code"] = rows["code"].astype(str).str.zfill(6)
    rows["date"] = pd.to_datetime(rows["date"].astype(str), format="%Y%m%d", errors="raise")
    for field in ("open", "high", "low", "close", "volume", "amount"):
        rows[field] = pd.to_numeric(rows[field], errors="coerce")
    rows["is_st"] = rows["is_st"].map({True: True, False: False, "True": True, "False": False})
    if rows.duplicated(["code", "date"]).any():
        raise ValueError("StockDB eligibility snapshot has duplicate code/date rows")
    return rows.sort_values(["date", "code"], kind="stable").reset_index(drop=True)


def _load_listing_master(path: Path | None) -> tuple[pd.DataFrame, dict]:
    if path is None:
        return pd.DataFrame({"ipoDate": pd.Series(dtype="datetime64[ns]"),
                             "outDate": pd.Series(dtype="datetime64[ns]")}), {
            "status": "missing", "path": None, "coverage": "none"
        }
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    master = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"code", "ipoDate", "outDate"}
    if not required <= set(master.columns) or master.code.duplicated().any():
        raise ValueError("Listing master must contain unique code/ipoDate/outDate fields")
    master = master.copy()
    master["code"] = master["code"].astype(str)
    master["ipoDate"] = pd.to_datetime(master["ipoDate"].replace("", pd.NA), errors="coerce")
    master["outDate"] = pd.to_datetime(master["outDate"].replace("", pd.NA), errors="coerce")
    return master.set_index("code"), {
        "status": "retrieval_time_overlay",
        "path": str(path),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "coverage": "measured_by_join",
    }


def _eligibility_rows(rows: pd.DataFrame, master: pd.DataFrame, *, min_amount: float,
                      min_listing_days: int, selection_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return a compact latest row table and daily coverage/eligibility counts."""

    rows = rows.copy()
    rows["symbol"] = rows["code"].map(_exchange_code)
    rows["observed_row"] = True
    rows["executable_proxy"] = (
        rows[["open", "high", "low", "close", "volume", "amount"]].gt(0).all(axis=1)
        & rows[["open", "high", "low", "close"]].notna().all(axis=1)
    )
    rows["st_known"] = rows["is_st"].notna()
    rows["st_clear"] = rows["is_st"].eq(False)
    lookup = master.reindex(rows["symbol"])
    rows["listing_date"] = pd.to_datetime(lookup["ipoDate"].to_numpy(), errors="coerce")
    rows["out_date"] = pd.to_datetime(lookup["outDate"].to_numpy(), errors="coerce")
    rows["listing_known"] = rows["listing_date"].notna()
    rows["out_date_known"] = rows["symbol"].isin(master.index)
    rows["age_days"] = (rows["date"] - rows["listing_date"]).dt.days
    rows["listing_age_ok"] = rows["age_days"].ge(min_listing_days)
    rows["not_retired"] = rows["out_date"].isna() | rows["date"].lt(rows["out_date"])
    first_observed = rows.groupby("symbol")["date"].transform("min")
    rows["first_observed_date"] = first_observed
    rows["observed_age_days"] = (rows["date"] - first_observed).dt.days
    rows["observed_listing_age_ok"] = rows["observed_age_days"].ge(min_listing_days)

    amounts = rows.pivot(index="date", columns="symbol", values="amount").sort_index()
    medians = amounts.rolling(20, min_periods=20).median()
    rows["median_amount_20"] = [medians.at[d, s] for d, s in zip(rows["date"], rows["symbol"])]
    rows["liquidity_known"] = rows["median_amount_20"].notna()
    rows["liquidity_ok"] = rows["median_amount_20"].ge(min_amount)
    rows["tradestatus_known"] = False
    rows["compatibility_known"] = (
        rows["executable_proxy"] & rows["st_known"] & rows["listing_known"]
        & rows["out_date_known"] & rows["liquidity_known"]
    )
    rows["eligible_proxy"] = (
        rows["compatibility_known"] & rows["st_clear"] & rows["listing_age_ok"]
        & rows["not_retired"] & rows["liquidity_ok"]
        & rows["date"].gt(pd.Timestamp(selection_date))
    )
    rows["observed_compatibility_known"] = (
        rows["executable_proxy"] & rows["st_known"] & rows["liquidity_known"]
    )
    rows["observed_eligible_proxy"] = (
        rows["observed_compatibility_known"] & rows["st_clear"]
        & rows["observed_listing_age_ok"] & rows["liquidity_ok"]
        & rows["date"].gt(pd.Timestamp(selection_date))
    )

    session_expected = rows["symbol"].nunique()
    daily = rows.groupby("date", sort=True).agg(
        observed_rows=("code", "size"),
        observed_symbols=("symbol", "nunique"),
        executable_proxy_rows=("executable_proxy", "sum"),
        st_known_rows=("st_known", "sum"),
        st_clear_rows=("st_clear", "sum"),
        listing_known_rows=("listing_known", "sum"),
        liquidity_known_rows=("liquidity_known", "sum"),
        compatibility_known_rows=("compatibility_known", "sum"),
        eligible_proxy_rows=("eligible_proxy", "sum"),
        observed_eligible_proxy_rows=("observed_eligible_proxy", "sum"),
    ).reset_index()
    daily["expected_symbols"] = session_expected
    daily["observed_coverage"] = daily.observed_symbols / session_expected
    return rows, daily


def build(snapshot: Path, listing_master: Path | None = None, output: Path | None = None,
          *, min_amount: float = DEFAULT_MIN_AMOUNT,
          min_listing_days: int = DEFAULT_MIN_LISTING_DAYS,
          selection_date: str = DEFAULT_SELECTION_DATE) -> tuple[Path, dict]:
    snapshot = Path(snapshot).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_chunks(snapshot, manifest)
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("A complete, unadmitted StockDB snapshot is required")
    rows = _load_rows(snapshot, manifest)
    master, master_meta = _load_listing_master(listing_master)
    enriched, daily = _eligibility_rows(rows, master, min_amount=min_amount,
                                         min_listing_days=min_listing_days,
                                         selection_date=selection_date)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_eligibility_compatibility" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_eligibility_compatibility" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    latest_day = enriched.date.max()
    latest = enriched.loc[enriched.date.eq(latest_day)].copy()
    latest_columns = ["code", "symbol", "date", "is_st", "open", "high", "low", "close", "volume", "amount",
                      "median_amount_20", "listing_date", "out_date", "age_days", "listing_age_ok",
                      "first_observed_date", "observed_age_days", "observed_listing_age_ok",
                      "not_retired", "executable_proxy", "tradestatus_known", "listing_known",
                      "out_date_known", "st_known", "liquidity_known", "liquidity_ok",
                      "compatibility_known", "eligible_proxy", "observed_compatibility_known",
                      "observed_eligible_proxy"]
    latest[latest_columns].to_csv(artifacts / "latest_eligibility_observation.csv", index=False)
    daily.to_csv(artifacts / "daily_eligibility_coverage.csv", index=False)
    expected_grid = int(enriched["symbol"].nunique() * daily.shape[0])
    observed = len(enriched)
    latest_listing_known = int(latest.listing_known.sum())
    latest_compat = int(latest.compatibility_known.sum())
    latest_observed_compat = int(latest.observed_compatibility_known.sum())
    result = {
        "schema": "stockdb_eligibility_compatibility_v1",
        "status": "completed_research_only_eligibility_compatibility",
        "snapshot": str(snapshot),
        "snapshot_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "listing_master": master_meta,
        "source_fields": list(REQUIRED_FIELDS),
        "missing_required_source_fields": ["tradestatus", "point_in_time_listing_date", "point_in_time_out_date"],
        "parameters": {"min_amount": float(min_amount), "min_listing_days": int(min_listing_days),
                       "selection_date": selection_date},
        "sessions": int(daily.shape[0]),
        "latest_session": latest_day.strftime("%Y-%m-%d"),
        "symbols": int(enriched.symbol.nunique()),
        "observed_rows": observed,
        "expected_grid_rows": expected_grid,
        "observed_grid_coverage": observed / expected_grid if expected_grid else 0.0,
        "latest_observed_rows": int(len(latest)),
        "latest_listing_known_rows": latest_listing_known,
        "latest_listing_unknown_rows": int(len(latest) - latest_listing_known),
        "latest_listing_age_unknown_rows": int(latest.age_days.isna().sum()),
        "latest_listing_age_below_min_rows": int((latest.age_days < min_listing_days).sum()),
        "latest_retired_rows": int((~latest.not_retired).sum()),
        "latest_st_known_rows": int(latest.st_known.sum()),
        "latest_st_clear_rows": int(latest.st_clear.sum()),
        "latest_executable_proxy_rows": int(latest.executable_proxy.sum()),
        "latest_liquidity_known_rows": int(latest.liquidity_known.sum()),
        "latest_liquidity_below_min_rows": int((latest.median_amount_20 < min_amount).sum()),
        "latest_liquidity_unknown_rows": int(latest.median_amount_20.isna().sum()),
        "latest_compatibility_known_rows": latest_compat,
        "latest_eligible_proxy_rows": int(latest.eligible_proxy.sum()),
        "latest_observed_listing_age_below_min_rows": int((latest.observed_age_days < min_listing_days).sum()),
        "latest_observed_compatibility_known_rows": latest_observed_compat,
        "latest_observed_eligible_proxy_rows": int(latest.observed_eligible_proxy.sum()),
        "all_rows_listing_known": int(enriched.listing_known.sum()),
        "all_rows_st_known": int(enriched.st_known.sum()),
        "all_rows_liquidity_known": int(enriched.liquidity_known.sum()),
        "all_rows_compatibility_known": int(enriched.compatibility_known.sum()),
        "all_rows_eligible_proxy": int(enriched.eligible_proxy.sum()),
        "all_rows_observed_eligible_proxy": int(enriched.observed_eligible_proxy.sum()),
        "tradestatus_field": {"present": False, "status": "missing_source_field",
                               "proxy_used": "positive_OHLC_volume_amount_only"},
        "quality_gate": "diagnostic_only",
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "execution_admitted": False,
        "artifacts": str(artifacts),
        "limitations": [
            "StockDB is_st is a source flag and was not independently exchange-reconciled.",
            "tradestatus is absent; executable_proxy is not a tradability or order admission decision.",
            "The listing overlay is retrieval-time metadata and not PIT verified for each observation date.",
            "No score, ranking, target holdings, paper orders, account, Ledger or THS state was accessed.",
        ],
    }
    (out / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB eligibility compatibility receipt", "",
        "Research-only diagnostic; no score, ranking, target holdings, paper orders or THS package.",
        f"- Snapshot: `{snapshot}`; latest session `{result['latest_session']}`.",
        f"- Grid coverage: `{observed:,}/{expected_grid:,}` = `{result['observed_grid_coverage']:.2%}`.",
        f"- Latest rows: `{len(latest):,}`; listing-date known `{latest_listing_known:,}`; compatibility-known `{latest_compat:,}`; eligible proxy `{result['latest_eligible_proxy_rows']:,}`.",
        f"- Conservative observed-history eligible proxy: `{result['latest_observed_eligible_proxy_rows']:,}`; this uses each symbol's first observed date as an upper bound on its listing date.",
        "- `tradestatus` and PIT listing status are missing; positive OHLC/volume/amount is retained only as a proxy diagnostic.",
        f"- Artifacts: `{artifacts}`.",
    ]) + "\n", encoding="utf-8")
    return out / "receipt.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--listing-master", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--min-amount", type=float, default=DEFAULT_MIN_AMOUNT)
    parser.add_argument("--min-listing-days", type=int, default=DEFAULT_MIN_LISTING_DAYS)
    parser.add_argument("--selection-date", default=DEFAULT_SELECTION_DATE)
    args = parser.parse_args(argv)
    path, result = build(args.snapshot, args.listing_master, args.out,
                          min_amount=args.min_amount, min_listing_days=args.min_listing_days,
                          selection_date=args.selection_date)
    print(json.dumps({"receipt": str(path), **{k: result[k] for k in (
        "status", "latest_session", "symbols", "sessions", "observed_rows",
        "latest_compatibility_known_rows", "latest_eligible_proxy_rows")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
