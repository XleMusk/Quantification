"""Compute fixed annual accounting candidates at actual system availability."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .annual_financial_inputs import FIELDS, LIMITS, select_asof
from .experiment import write_json


ID = "annual_accounting_quality_candidates_v1"
INVESTMENT_ID = "annual_accounting_quality_investment_candidates_v1"
INVESTMENT_SOURCES = [
    "https://global-q.org/uploads/1/2/2/6/122679606/portfoliostd_2024april.pdf",
    "https://global-q.org/uploads/1/2/2/6/122679606/factorstd_2025feb.pdf",
]
DEFINITIONS = {
    "revenue_cost_spread_assets": {
        "fields": ["revenue", "operating_cost", "total_assets"],
        "denominator": "total_assets",
        "formula": "(TOTAL_OPERATE_INCOME - OPERATE_COST) / TOTAL_ASSETS",
        "boundary": "Total operating revenue less source operating cost, not independently reconciled gross profit/COGS",
    },
    "operating_cashflow_assets": {
        "fields": ["operating_cashflow", "total_assets"],
        "denominator": "total_assets",
        "formula": "NETCASH_OPERATE / TOTAL_ASSETS",
        "boundary": "Reported net operating cashflow; not the AQR QMJ cashflow-component formula",
    },
    "revenue_cost_spread_margin": {
        "fields": ["revenue", "operating_cost"],
        "denominator": "revenue",
        "formula": "(TOTAL_OPERATE_INCOME - OPERATE_COST) / TOTAL_OPERATE_INCOME",
        "boundary": "Not independently verified gross margin or industry-comparable bank profitability",
    },
}
INVESTMENT_DEFINITION = {
    "fields": ["total_assets"], "prior_fields": ["total_assets"],
    "formula": "TOTAL_ASSETS[y] / TOTAL_ASSETS[y-1] - 1",
    "boundary": "Exact preceding December year-end, same source identity, both positive asset values and actual receipt times; not expected growth or a q-factor return replication",
    "sources": INVESTMENT_SOURCES,
}
FEATURE_LIMITS = LIMITS + [
    "Fixed formulas, positive denominators; negative and zero numerators are retained without clipping",
    "Each feature has its own required inputs and retrieval time; absent cost does not erase known operating cashflow",
    "Year-end assets, not inferred average assets; no growth, accruals, complete QMJ or return optimization",
    "Feature-ready identities are not a tradable universe, target holdings, predictive evidence or execution approval",
    "No ranking, normalization, imputation, weighting, historical signal backfill, account replay or broker operation",
]


def asset_growth(panel, frame, period, asof):
    prior_period = f"{pd.Timestamp(period).year - 1:04d}-12-31"
    _, now = select_asof(frame, period, asof, ["total_assets"])
    _, prior = select_asof(panel, prior_period, asof, ["total_assets"])
    # Align by identity and the exact previous year, never the previous available row.
    prior = prior.set_index("symbol").reindex(frame.symbol)
    prior.index = frame.index
    missing = prior.report_day.isna()
    ready = now.required_input_ready_asof & prior.required_input_ready_asof.eq(True)
    positive = frame.total_assets.gt(0) & prior.total_assets.gt(0)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        value = frame.total_assets / prior.total_assets.where(ready & positive) - 1
    finite = value.notna() & np.isfinite(value)
    status = pd.Series("available_numeric", index=frame.index)
    status.loc[ready & positive & ~finite] = "nonfinite_ratio"
    status.loc[ready & ~positive] = "nonpositive_denominator"
    status.loc[~ready] = "unavailable_asof"
    status.loc[missing] = "missing_prior_period"
    received = pd.concat([pd.to_datetime(member.balance_retrieved_at, utc=True, errors="coerce")
                          for member in (frame, prior)], axis=1)
    frame["asset_growth"] = value.where(ready & positive & finite)
    frame["asset_growth_status"] = status
    frame["asset_growth_received_at"] = received.max(axis=1).where(received.notna().all(axis=1))
    frame["asset_growth_prior_period"] = prior_period
    frame["asset_growth_prior_assets"] = prior.total_assets
    frame["asset_growth_prior_received_at"] = received.iloc[:, 1]
    frame["asset_growth_prior_notice_at"] = prior.balance_notice_at
    frame["asset_growth_missing_prior_period"] = missing
    for suffix, column in (("unknown_input", "unknown_required_field"),
                           ("not_received_by_asof", "not_received_by_asof"),
                           ("notice_unknown_or_later", "publication_day_unknown_or_later")):
        frame["asset_growth_" + suffix] = now[column] | prior[column].ne(False)


def compute(panel, asof, *, include_investment=False):
    if panel.duplicated(["symbol", "report_day"]).any():
        raise ValueError("Unique annual security/period inputs required")
    periods = []
    for period, members in panel.groupby("report_day", sort=True):
        frame = members.copy()
        for name, spec in DEFINITIONS.items():
            _, availability = select_asof(members, period, asof, spec["fields"])
            denominator = frame[spec["denominator"]]
            numerator = (frame.operating_cashflow if name == "operating_cashflow_assets"
                         else frame.revenue - frame.operating_cost)
            positive = denominator.gt(0)
            ready = availability.required_input_ready_asof & positive
            with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
                value = numerator / denominator.where(ready)
            finite = value.notna() & np.isfinite(value)
            status = pd.Series("available_numeric", index=frame.index)
            status.loc[ready & ~finite] = "nonfinite_ratio"
            status.loc[availability.required_input_ready_asof & ~positive] = "nonpositive_denominator"
            status.loc[~availability.required_input_ready_asof] = "unavailable_asof"
            frame[name] = value.where(ready & finite)
            frame[name + "_status"] = status
            reports = [report for report, fields in FIELDS.items() if set(fields) & set(spec["fields"])]
            received = pd.concat([pd.to_datetime(frame[report + "_retrieved_at"], utc=True, errors="coerce")
                                  for report in reports], axis=1)
            frame[name + "_received_at"] = received.max(axis=1).where(received.notna().all(axis=1))
            frame[name + "_unknown_input"] = availability.unknown_required_field
            frame[name + "_not_received_by_asof"] = availability.not_received_by_asof
            frame[name + "_notice_unknown_or_later"] = availability.publication_day_unknown_or_later
        if include_investment:
            asset_growth(panel, frame, period, asof)
        frame["asof"] = pd.Timestamp(asof).isoformat()
        periods.append(frame)
    if not periods:
        raise ValueError("Nonempty annual inputs required")
    return pd.concat(periods, ignore_index=True)


def build(input_run, asof, *, include_investment=False):
    input_run = Path(input_run).resolve()
    source = json.loads((input_run / "result.json").read_text(encoding="utf-8"))
    if source["status"] != "completed_annual_input_consumer" or source["execution_admitted"] is not False:
        raise ValueError("Completed unadmitted annual input consumer required")
    panel = pd.read_parquet(input_run / "annual_inputs.parquet")
    if len(panel) != source["rows"] or sorted(panel.report_day.unique()) != source["periods"]:
        raise ValueError("Annual input period/row count disagrees with its source")
    features = compute(panel, asof, include_investment=include_investment)
    definitions = DEFINITIONS | ({"asset_growth": INVESTMENT_DEFINITION} if include_investment else {})
    limits = FEATURE_LIMITS if not include_investment else [
        value for value in FEATURE_LIMITS if "no growth, accruals" not in value] + [
        "Year-end quality ratios plus exact consecutive-year asset growth; no accruals, complete QMJ or expected-growth forecasting",
        "Both years use the frozen retrieved version; no claim about historical availability or restatement comparability",
        "Missing preceding years remain missing; zero/negative assets do not become usable investment ratios",
    ]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/annual_quality_features" / identifier
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, root / "implementation.py")
    features.to_parquet(root / "annual_quality_features.parquet", index=False)
    profiles = []
    for period, members in features.groupby("report_day", sort=True):
        for name in definitions:
            values = members[name].dropna()
            profiles.append({"report_day": period, "feature": name, "source_rows": len(members),
                "available_numeric": len(values), "negative": int(values.lt(0).sum()),
                "observed_zero": int(values.eq(0).sum()),
                "unavailable_asof": int(members[name + "_status"].eq("unavailable_asof").sum()),
                "nonpositive_denominator": int(members[name + "_status"].eq("nonpositive_denominator").sum()),
                "nonfinite_ratio": int(members[name + "_status"].eq("nonfinite_ratio").sum()),
                "missing_prior_period": int(members[name + "_status"].eq("missing_prior_period").sum()),
                "median": float(values.median()) if len(values) else None})
    pd.DataFrame(profiles).to_csv(root / "feature_coverage.csv", index=False)
    receipt = {"id": identifier, "definition_id": INVESTMENT_ID if include_investment else ID, "status": "completed_annual_quality_features",
        "input_run": str(input_run), "asof": pd.Timestamp(asof).isoformat(), "rows": len(features),
        "securities": int(features.symbol.nunique()), "definitions": definitions,
        "profiles": profiles, "execution_admitted": False, "pit_verified": False,
        "units_independently_verified": False, "limits": limits}
    write_json(root / "result.json", receipt)
    out = ROOT / "outputs/annual_quality_features" / identifier
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", receipt | {"artifacts": str(root)})
    lines = ["# Annual Accounting Quality Candidates", "", "NO_TRADE. Actual fields, no predictive/return result.",
             f"As of {receipt['asof']}; {len(features)} security/year rows, {receipt['securities']} source identities.", "",
             "|Period|Feature|Numeric|Negative|Known zero|Unavailable|Nonpositive denominator|Nonfinite|Missing prior year|",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    lines += ["|" + "|".join(str(row[key]) for key in ("report_day", "feature", "available_numeric", "negative",
              "observed_zero", "unavailable_asof", "nonpositive_denominator", "nonfinite_ratio", "missing_prior_period")) + "|" for row in profiles]
    lines += ["", *[f"- {name}: {spec['formula']}. {spec['boundary']}" for name, spec in definitions.items()],
              "", *["- " + value for value in limits], "", "Actual features: " + str(root / "annual_quality_features.parquet")]
    if include_investment:
        lines += ["", *["- Primary method reference: " + value for value in INVESTMENT_SOURCES]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("id", "asof", "rows", "securities", "profiles")}), flush=True)
    print(str(root), flush=True)
    return root, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--include-investment", action="store_true")
    args = parser.parse_args(argv)
    build(args.input_run, args.as_of, include_investment=args.include_investment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
