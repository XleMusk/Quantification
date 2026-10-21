"""Read frozen annual targets and saved integer capacity, without re-running either."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..rules import cents
from ..settings import ROOT
from .annual_quality_portfolios import declared_definition, load_quality
from .experiment import write_json


ID = "annual_saved_target_capacity_exposures_v1"
REPORTS = ("balance", "income", "cashflow")
# Exact observed provider labels, not a new independently verified classification.
FINANCIAL_LABELS = {"\u94f6\u884c\u2161", "\u8bc1\u5238\u2161", "\u4fdd\u9669\u2161", "\u591a\u5143\u91d1\u878d"}
LIMITS = [
    "Source industry names are frozen retrieval metadata, not independent or historical PIT classifications",
    "Require three matching nonblank statement labels and their actual receipt/publication availability",
    "Missing/conflicting/late metadata stay unknown; no security-name guessing or financial-sector exclusion",
    "Compare original fixed weights with saved integer capacity only; no new target, account, fill or quote",
    "Stock-normalized shares describe concentration, not covariance-based portfolio risk or industry neutrality",
    "Weighted scores are original common-pool diagnostics, not return attribution or predictive evidence",
    "Known September marks and synthetic capital, not current client cash/permissions or post-declaration sessions",
    "No new weights, financial backfill, historical replay, cost calculation, strategy selection or broker operation",
]


def industry_metadata(annual, asof):
    point = pd.Timestamp(asof)
    if point.tzinfo is None or annual.symbol.duplicated().any():
        raise ValueError("Unique annual identities and an explicit as-of timezone required")
    labels, available = [], pd.Series(True, index=annual.index)
    for report in REPORTS:
        get = lambda suffix: annual.get(report + suffix, pd.Series(None, index=annual.index, dtype=object))
        label = get("_industry_name_source").astype("string").str.strip()
        label = label.mask(label.str.lower().isin(["", "none", "nan", "null"]))
        labels.append(label)
        received = pd.to_datetime(get("_retrieved_at"), errors="coerce", utc=True)
        notice = pd.to_datetime(get("_notice_at"), errors="coerce")
        available &= received.notna() & received.le(point.tz_convert("UTC"))
        available &= notice.notna() & notice.dt.date.le(point.tz_convert("Asia/Hong_Kong").date())
    frame = pd.concat(labels, axis=1)
    frame.columns = REPORTS
    missing = frame.isna().any(axis=1)
    conflict = frame.nunique(axis=1, dropna=True).gt(1)
    known = available & ~missing & ~conflict
    status = pd.Series("consistent_source_metadata", index=annual.index)
    status.loc[conflict] = "conflicting_statement_labels"
    status.loc[missing] = "missing_statement_label"
    status.loc[~available] = "metadata_not_available_asof"
    return pd.DataFrame({"symbol": annual.symbol, "source_industry": frame.balance.where(known),
                         "industry_status": status,
                         "financial_source_label": frame.balance.isin(FINANCIAL_LABELS).where(known),
                         "industry_independently_verified": False})


def exposure_profile(members, weights):
    if not set(weights) <= set(members.index):
        raise ValueError("Every exposure identity must belong to the declared common pool")
    held = members.reindex(list(weights)).copy()
    held["weight"] = [float(weights[symbol]) for symbol in held.index]
    if not np.isfinite(held.weight).all() or held.weight.lt(0).any() or held.weight.sum() > 1 + 1e-12:
        raise ValueError("Finite nonnegative long-only exposure weights required")
    held = held.loc[held.weight.gt(0)].copy()
    gross = float(held.weight.sum())
    held["stock_share"] = held.weight / gross if gross else 0.
    sectors = []
    for label, rows in held.groupby("source_industry", dropna=False, sort=False):
        sectors.append({"source_industry": None if pd.isna(label) else str(label), "names": len(rows),
                        "gross_weight": float(rows.weight.sum()), "stock_share": float(rows.stock_share.sum())})
    sectors.sort(key=lambda row: (-row["stock_share"], row["source_industry"] or ""))
    name_hhi = float((held.stock_share ** 2).sum()) if gross else None
    known = held.source_industry.notna()
    known_sectors = [row for row in sectors if row["source_industry"] is not None]
    weighted = {}
    for field in ("quality", "investment", "momentum", "volatility"):
        if field in held:
            weighted[field] = float((held[field] * held.stock_share).sum()) if gross and np.isfinite(held[field]).all() else None
    return {"gross": gross, "positive_names": len(held), "known_industry_names": int(known.sum()),
            "unknown_industry_gross": float(held.loc[~known, "weight"].sum()),
            "source_financial_names": int(held.financial_source_label.eq(True).sum()),
            "source_financial_gross": float(held.loc[held.financial_source_label.eq(True), "weight"].sum()),
            "known_industries": len(known_sectors), "largest_known_industry": known_sectors[0] if known_sectors else None,
            "name_hhi_stock_normalized": name_hhi, "effective_names": 1 / name_hhi if name_hhi else None,
            "label_hhi_with_unknown_bucket": sum(row["stock_share"] ** 2 for row in sectors) if gross else None,
            "weighted_original_diagnostics": weighted, "industries": sectors}


def compare_profiles(members, target, sized):
    original, allocated = exposure_profile(members, target), exposure_profile(members, sized)
    left = {symbol: weight / original["gross"] for symbol, weight in target.items()} if original["gross"] else {}
    right = {symbol: weight / allocated["gross"] for symbol, weight in sized.items()} if allocated["gross"] else {}
    drift = .5 * sum(abs(left.get(symbol, 0) - right.get(symbol, 0)) for symbol in set(left) | set(right))
    return {"target": original, "saved_capacity": allocated, "stock_normalized_weight_distance": drift,
            "gross_difference": allocated["gross"] - original["gross"]}


def build(prepared_run, capacity_run):
    root, capacity_root = Path(prepared_run).resolve(), Path(capacity_run).resolve()
    capacity_root.relative_to(root / "capacity_previews")
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    cases, investment = declared_definition(protocol)
    preview = json.loads((root / "preview_weights.json").read_text(encoding="utf-8"))
    capacity = json.loads((capacity_root / "result.json").read_text(encoding="utf-8"))
    if (preview["mode"] != "known_market_preview_only" or preview["held"] != [] or preview["account_created"] is not False
            or list(preview["cases"]) != list(cases) or protocol["execution_admitted"] is not False
            or capacity["status"] not in {"completed_known_price_allocation_diagnostic", "completed_known_price_capacity_diagnostic"}
            or capacity["definition_id"] != protocol["definition_id"] or Path(capacity["prepared_run"]).resolve() != root
            or capacity["portfolio"] != protocol["portfolio"] or capacity["market_day"] != protocol["preview_market_day"]
            or capacity["financial_asof"] != protocol["feature_asof"] or capacity["declared_at"] != protocol["declared_at"]
            or capacity["account_created"] is not False or capacity["execution_admitted"] is not False
            or capacity["orders"] != 0 or capacity["fills"] != 0):
        raise ValueError("The original complete no-account capacity of this frozen annual comparison is required")
    annual, receipt = load_quality(protocol["quality_run"], protocol["report_day"], investment)
    if receipt["asof"] != protocol["feature_asof"] or receipt["id"] != protocol["feature_id"]:
        raise ValueError("The frozen financial retrieval version cannot change for exposure diagnostics")
    pool = pd.read_parquet(root / "preview_common_pool.parquet")
    if pool.symbol.duplicated().any() or len(pool) != protocol["preview_counts"]["common_pool"]:
        raise ValueError("Original common-pool identities/count required")
    metadata = industry_metadata(annual, protocol["feature_asof"])
    members = pool.merge(metadata, on="symbol", validate="one_to_one", how="left").set_index("symbol")
    slots = pd.read_csv(capacity_root / "capacity.csv")
    if slots.duplicated(["case", "symbol"]).any() or set(slots.case) != set(cases):
        raise ValueError("Distinct original capacity slots required")
    summaries, joined = [], []
    for case in cases:
        rows = slots.loc[slots.case.eq(case)].copy()
        target = preview["cases"][case]
        summary = next(row for row in capacity["cases"] if row["case"] == case)
        weight_column = "original_target_weight" if "original_target_weight" in rows else "target_weight"
        if (set(rows.symbol) != set(target) or summary["unknown_targets"] != 0
                or not rows.market_day.eq(protocol["preview_market_day"]).all()
                or not rows.status.isin(["known_close_size", "below_minimum_entry_size"]).all()
                or not np.allclose(rows[weight_column], rows.symbol.map(target), atol=1e-12, rtol=0)
                or rows.notional_cents.isna().any() or rows.notional_cents.lt(0).any()
                or not rows.notional_cents.mod(1).eq(0).all()):
            raise ValueError("Capacity exposure cannot substitute targets or invent unknown sizing")
        sized = dict(zip(rows.symbol, rows.notional_cents / cents(protocol["portfolio"]["initial_cash"])))
        if (not np.allclose(rows.rounded_weight, rows.symbol.map(sized), atol=1e-12, rtol=0)
                or not np.isclose(sum(sized.values()), summary["rounded_gross"], atol=1e-12, rtol=0)):
            raise ValueError("Saved capacity weights do not reconcile with its original notionals/gross")
        comparison = compare_profiles(members, target, sized)
        summaries.append({"case": case, **comparison})
        rows["original_target_weight"] = rows.symbol.map(target)
        joined.append(rows.merge(members.reset_index(), on="symbol", how="left", validate="one_to_one"))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    destination = root / "exposure_previews" / identifier
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, destination / "implementation.py")
    pd.concat(joined, ignore_index=True).to_parquet(destination / "slots.parquet", index=False)
    members.reset_index().to_parquet(destination / "common_pool.parquet", index=False)
    pool_profile = exposure_profile(members, dict.fromkeys(members.index, 1 / len(members)))
    result = {"id": identifier, "consumer_id": ID, "status": "completed_saved_annual_exposures",
              "prepared_run": str(root), "definition_id": protocol["definition_id"],
              "capacity_run": str(capacity_root), "capacity_id": capacity["id"],
              "allocation_policy": capacity.get("allocation_policy"),
              "market_day": protocol["preview_market_day"], "financial_asof": protocol["feature_asof"],
              "report_day": protocol["report_day"], "source_metadata_statuses": metadata.industry_status.value_counts().to_dict(),
              "common_pool": pool_profile, "cases": summaries, "account_created": False,
              "orders": 0, "fills": 0, "execution_admitted": False, "limits": LIMITS}
    write_json(destination / "result.json", result)
    out = ROOT / "outputs/annual_quality_portfolios" / protocol["id"] / "exposures" / identifier
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "run.json", result | {"artifacts": str(destination)})
    lines = ["# Saved Annual Target / Capacity Exposures", "", "NO_TRADE. " + result["status"],
             "No ranking, allocation or account was re-run. All exposures read the declared targets and saved capacity.",
             f"Known raw-price date: {result['market_day']}; metadata as-of: {result['financial_asof']}.",
             f"Common pool {len(members)}; source-financial names {pool_profile['source_financial_names']}; "
             f"unknown source-industry names {len(members) - pool_profile['known_industry_names']}.", "",
             "|Case|Target -> sized names|Target -> sized gross|Effective names after sizing|Weight distance|Largest known label after sizing (stock share)|",
             "|---|---:|---:|---:|---:|---|"]
    for row in summaries:
        before, after = row["target"], row["saved_capacity"]
        largest = after["largest_known_industry"]
        label = f"{largest['source_industry']} ({largest['stock_share']:.2%})" if largest else "unknown"
        effective = f"{after['effective_names']:.2f}" if after["effective_names"] else "unknown"
        lines.append(f"|{row['case']}|{before['positive_names']} -> {after['positive_names']}|"
                     f"{before['gross']:.2%} -> {after['gross']:.2%}|{effective}|"
                     f"{row['stock_normalized_weight_distance']:.2%}|{label}|")
    lines += ["", *["- " + line for line in LIMITS], "", "Artifacts: " + str(destination)]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("id", "status", "capacity_id", "market_day",
                      "financial_asof", "account_created", "orders", "fills")}, ensure_ascii=False), flush=True)
    print(str(out / "REPORT.md"), flush=True)
    return destination, result


def saved_exposures(root, case):
    parent = root / "exposure_previews"
    values = []
    if not parent.exists():
        return values
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    for path in sorted(parent.iterdir(), reverse=True):
        if not (path / "result.json").is_file():
            continue
        path.resolve().relative_to(root)
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        if (result["consumer_id"] != ID or result["status"] != "completed_saved_annual_exposures"
                or Path(result["prepared_run"]).resolve() != root or result["definition_id"] != protocol["definition_id"]
                or result["account_created"] is not False or result["execution_admitted"] is not False
                or result["orders"] != 0 or result["fills"] != 0):
            raise ValueError("Exposure record is not this fixed no-account diagnostic")
        summary = next(row for row in result["cases"] if row["case"] == case)
        slots = pd.read_parquet(path / "slots.parquet")
        slots = slots.loc[slots.case.eq(case)]
        values.append({"id": result["id"], "capacity_id": result["capacity_id"],
                       "financial_asof": result["financial_asof"], "summary": summary,
                       "common_pool": result["common_pool"],
                       "slots": slots.astype(object).where(pd.notna(slots), None).to_dict("records")})
    return values
