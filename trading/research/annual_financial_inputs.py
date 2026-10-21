"""Join actual annual statements by security/period without historical availability backfill."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from a_stock_committee.market_data import normalize_symbol
from ..settings import DATA_ROOT, ROOT
from .bulk_annual_financials import ENDPOINT, REPORTS
from .experiment import write_json


FIELDS = {
    "balance": {"total_assets": "TOTAL_ASSETS", "total_liabilities": "TOTAL_LIABILITIES",
                "total_equity": "TOTAL_EQUITY", "monetary_funds": "MONETARYFUNDS", "parent_equity": None},
    "income": {"revenue": "TOTAL_OPERATE_INCOME", "parent_net_profit": "PARENT_NETPROFIT",
               "total_net_profit": None, "operating_cost": "OPERATE_COST", "pretax_profit": "TOTAL_PROFIT",
               "income_tax": "INCOME_TAX"},
    "cashflow": {"operating_cashflow": "NETCASH_OPERATE"},
}
UNIT_DOC = "https://akshare.akfamily.xyz/data/stock/stock.html"
DOCUMENTED_CNY_FIELDS = {"total_assets", "total_liabilities", "total_equity", "monetary_funds",
                        "revenue", "parent_net_profit", "operating_cost", "pretax_profit", "operating_cashflow"}
LIMITS = [
    "Current retrieval versions, not original historical disclosures or version-safe PIT",
    "Source financial identities may include pre-listing or delisted histories, not a historical/current tradable universe",
    "Outer joins retain missing statements and each original statement publication/retrieval time",
    "Total operating revenue and parent profit remain separate from total net profit and parent equity",
    "Missing source fields, nulls and nonfinite values stay unknown; observed zero is retained",
    "Primary equality certifies only that exact issuer/year/field, not all-source currency/scope/units",
    "Upstream documents the named monetary amounts in RMB; documentation is not independent issuer verification",
    "Source industries are retrieval metadata, not a verified historical industry classification",
    "No historical factor backfill, TTM/annualization, model/strategy change, replay or broker operation; NO_TRADE",
]


def join_period(tables, period):
    panel = None
    for report, mapping in FIELDS.items():
        frame = tables[report]
        if frame.SECURITY_CODE.duplicated().any():
            raise ValueError("Annual statement identities are not unique")
        if not pd.to_datetime(frame.REPORT_DATE, errors="raise").dt.strftime("%Y-%m-%d").eq(period).all():
            raise ValueError("Annual input source contains a different period")
        selected = pd.DataFrame({"security_code": frame.SECURITY_CODE,
            "symbol": frame.SECUCODE.map(lambda value: normalize_symbol(value).vt_symbol),
            report + "_statement_observed": True,
            report + "_notice_at": frame.NOTICE_DATE,
            report + "_retrieved_at": frame.available_to_system_at,
            report + "_industry_name_source": frame.INDUSTRY_NAME,
            report + "_report_type_source": frame.REPORT_TYPE_CODE,
            report + "_data_state_source": frame.DATA_STATE})
        if not selected.symbol.str[:6].eq(selected.security_code).all():
            raise ValueError("Source code and exchange identity conflict")
        for field, raw_key in mapping.items():
            if raw_key is None or raw_key not in frame:
                selected[field] = np.nan
                selected[field + "_status"] = "field_absent_from_source"
                continue
            raw = frame[raw_key]
            value = pd.to_numeric(raw, errors="coerce")
            finite = value.notna() & np.isfinite(value)
            status = pd.Series("invalid_numeric", index=frame.index)
            status.loc[raw.isna()] = "source_null"
            status.loc[raw.eq("")] = "source_blank"
            status.loc[finite] = "observed_numeric"
            selected[field] = value.where(finite)
            selected[field + "_status"] = status
        panel = selected if panel is None else panel.merge(selected, on=["security_code", "symbol"], how="outer", validate="one_to_one")
    for report, mapping in FIELDS.items():
        observed = panel[report + "_statement_observed"].eq(True)
        panel[report + "_statement_observed"] = observed
        for field in mapping:
            panel.loc[~observed, field + "_status"] = "statement_not_observed"
    panel["report_day"] = period
    panel["units_verified"] = False
    panel["pit_verified"] = False
    panel["execution_admitted"] = False
    return panel


def build(source_runs, primary_inputs=None):
    panels, inputs, periods = [], [], set()
    for source_run in source_runs:
        source_run = Path(source_run).resolve()
        plan = json.loads((source_run / "plan.json").read_text(encoding="utf-8"))
        result = json.loads((source_run / "result.json").read_text(encoding="utf-8"))
        if plan["source"] != ENDPOINT or plan["reports"] != REPORTS or result["execution_admitted"] is not False:
            raise ValueError("Existing public annual statement candidates required")
        if result["status"] != "completed_candidate_cache":
            raise ValueError("Finish the source requests before freezing their annual input consumer")
        inputs.append({"root": str(source_run), "id": plan["id"], "source_status": result["status"]})
        for period in plan["periods"]:
            if period in periods:
                raise ValueError("Duplicate annual period versions require explicit reconciliation")
            periods.add(period)
            tables = {report: pd.read_parquet(source_run / (period + "_" + report + ".parquet")) for report in FIELDS}
            for report, table in tables.items():
                group = result["groups"][period + "_" + report]
                if len(table) != group["count"] or group["count"] == 0:
                    raise ValueError("Complete nonempty source statement required for annual join")
            panels.append(join_period(tables, period))
    panel = pd.concat(panels, ignore_index=True)
    if panel.duplicated(["symbol", "report_day"]).any():
        raise ValueError("Annual input identities conflict across sources")
    comparisons = []
    if primary_inputs:
        primary_inputs = Path(primary_inputs).resolve()
        primary = pd.read_csv(primary_inputs, dtype={"symbol": str, "report_day": str})
        for row in primary.itertuples():
            selected = panel.loc[panel.symbol.eq(row.symbol) & panel.report_day.eq(row.report_day)]
            if len(selected) != 1 or row.field not in panel:
                comparisons.append({"symbol": row.symbol, "report_day": row.report_day, "field": row.field,
                                    "status": "not_in_candidate_input"})
                continue
            value = selected.iloc[0]
            observed = value[row.field + "_status"] == "observed_numeric"
            gap = float(value[row.field]) - float(row.value_cny) if observed else None
            comparisons.append({"symbol": row.symbol, "report_day": row.report_day, "field": row.field,
                "primary_value_cny": row.value_cny, "source_value_unscaled": value[row.field],
                "absolute_gap": abs(gap) if gap is not None else None,
                "status": "exact_same_value_as_cny" if gap == 0 else "value_conflict" if observed else value[row.field + "_status"]})
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/annual_financial_inputs" / identifier
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, root / "implementation.py")
    panel.to_parquet(root / "annual_inputs.parquet", index=False)
    coverage = []
    for period, members in panel.groupby("report_day", sort=True):
        for report, mapping in FIELDS.items():
            for field in mapping:
                coverage.append({"period": period, "field": field, "source_field": mapping[field],
                    "upstream_declared_unit": "CNY" if field in DOCUMENTED_CNY_FIELDS else None,
                    "statement": report, "source_union_rows": len(members),
                    "observed_numeric": int(members[field + "_status"].eq("observed_numeric").sum()),
                    "observed_zero": int(members[field].eq(0).sum()),
                    "statement_not_observed": int(members[field + "_status"].eq("statement_not_observed").sum()),
                    "source_null": int(members[field + "_status"].eq("source_null").sum()),
                    "field_absent_from_source": int(members[field + "_status"].eq("field_absent_from_source").sum())})
    pd.DataFrame(coverage).to_csv(root / "field_coverage.csv", index=False)
    pd.DataFrame(comparisons).to_csv(root / "primary_comparison.csv", index=False)
    all_statements = panel[[report + "_statement_observed" for report in FIELDS]].all(axis=1)
    metadata_gap = panel[[report + "_notice_at" for report in FIELDS]].nunique(axis=1, dropna=True).gt(1)
    receipt = {"id": identifier, "status": "completed_annual_input_consumer", "source_runs": inputs,
        "rows": len(panel), "securities": int(panel.symbol.nunique()), "periods": sorted(periods),
        "three_statements_rows": int(all_statements.sum()), "cross_statement_notice_differences": int(metadata_gap.sum()),
        "primary_inputs": str(primary_inputs) if primary_inputs else None,
        "source_unit_documentation": {"url": UNIT_DOC, "status": "official_upstream_field_documentation_checked",
                                     "documented_cny_fields": sorted(DOCUMENTED_CNY_FIELDS),
                                     "conversion_applied": False, "all_issuers_independently_verified": False},
        "primary_exact_fields": sum(row["status"] == "exact_same_value_as_cny" for row in comparisons),
        "primary_conflicts": sum(row["status"] == "value_conflict" for row in comparisons),
        "primary_unavailable_fields": sum(row["status"] not in ("exact_same_value_as_cny", "value_conflict") for row in comparisons),
        "execution_admitted": False, "pit_verified": False, "units_verified": False, "limits": LIMITS}
    write_json(root / "result.json", receipt)
    out = ROOT / "outputs/annual_financial_inputs" / identifier
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", receipt | {"artifacts": str(root)})
    lines = ["# Annual Financial Input Consumer", "", "Status: " + receipt["status"] + ". NO_TRADE.",
        f"Rows: {len(panel)}; securities: {receipt['securities']}; all-three-statement rows: {receipt['three_statements_rows']}.",
        f"Different source publication metadata: {receipt['cross_statement_notice_differences']} rows (not established issuer errors).",
        f"Primary exact values: {receipt['primary_exact_fields']}; conflicts: {receipt['primary_conflicts']}; unavailable source fields: {receipt['primary_unavailable_fields']}.", "",
        "|Period|Field|Union securities|Numeric|Known zero|Missing statement|Null|Absent field|",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    lines += ["|" + "|".join(str(row[column]) for column in ("period", "field", "source_union_rows", "observed_numeric",
              "observed_zero", "statement_not_observed", "source_null", "field_absent_from_source")) + "|" for row in coverage]
    lines += ["", *["- " + value for value in LIMITS], "", "Upstream unit definitions: " + UNIT_DOC,
              "Actual inputs: " + str(root / "annual_inputs.parquet")]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("id", "rows", "securities", "three_statements_rows", "primary_exact_fields", "primary_conflicts", "primary_unavailable_fields")}), flush=True)
    print(str(root), flush=True)
    return root, receipt


def select_asof(panel, period, asof, fields):
    point = pd.Timestamp(asof)
    if point.tzinfo is None:
        raise ValueError("As-of selection needs an explicit timezone")
    if not fields or len(set(fields)) != len(fields) or not set(fields) <= DOCUMENTED_CNY_FIELDS:
        raise ValueError("Select distinct upstream-documented annual monetary fields")
    if not period.endswith("-12-31") or pd.Timestamp(period).strftime("%Y-%m-%d") != period:
        raise ValueError("Canonical annual period required")
    members = panel.loc[panel.report_day.eq(period)].copy()
    ready = pd.Series(True, index=members.index)
    unknown = pd.Series(False, index=members.index)
    not_received = pd.Series(False, index=members.index)
    publication_unknown_or_later = pd.Series(False, index=members.index)
    reports = {report for report, mapping in FIELDS.items() if set(mapping) & set(fields)}
    for field in fields:
        finite = members[field].notna() & np.isfinite(members[field])
        unknown |= ~members[field + "_status"].eq("observed_numeric") | ~finite
    for report in reports:
        received = pd.to_datetime(members[report + "_retrieved_at"], utc=True, errors="coerce")
        not_received |= received.isna() | received.gt(point.tz_convert("UTC"))
        notice = pd.to_datetime(members[report + "_notice_at"], errors="coerce")
        publication_unknown_or_later |= notice.isna() | notice.dt.date.gt(point.tz_convert("Asia/Hong_Kong").date())
    ready &= ~(unknown | not_received | publication_unknown_or_later)
    if pd.Timestamp(period).date() > point.tz_convert("Asia/Hong_Kong").date():
        ready[:] = False
    members["required_input_ready_asof"] = ready
    members["unknown_required_field"] = unknown
    members["not_received_by_asof"] = not_received
    members["publication_day_unknown_or_later"] = publication_unknown_or_later
    return members.loc[ready].copy(), members


def materialize_asof(input_run, period, asof, fields):
    input_run = Path(input_run).resolve()
    source = json.loads((input_run / "result.json").read_text(encoding="utf-8"))
    if source["status"] != "completed_annual_input_consumer" or source["execution_admitted"] is not False:
        raise ValueError("Completed unadmitted annual input consumer required")
    if period not in source["periods"]:
        raise ValueError("Requested annual period is absent from this input consumer")
    panel = pd.read_parquet(input_run / "annual_inputs.parquet")
    selected, members = select_asof(panel, period, asof, fields)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/annual_financial_selection" / identifier
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, root / "implementation.py")
    selected.to_parquet(root / "available_annual_inputs.parquet", index=False)
    members[["symbol", "report_day", "required_input_ready_asof", "unknown_required_field",
             "not_received_by_asof", "publication_day_unknown_or_later"]].to_csv(root / "availability.csv", index=False)
    receipt = {"id": identifier, "status": "completed_annual_asof_consumer", "input_run": str(input_run),
        "asof": pd.Timestamp(asof).isoformat(), "report_day": period, "fields": fields, "source_rows": len(members),
        "available_rows": len(selected), "unknown_required_field_rows": int(members.unknown_required_field.sum()),
        "not_received_by_asof_rows": int(members.not_received_by_asof.sum()),
        "publication_day_unknown_or_later_rows": int(members.publication_day_unknown_or_later.sum()),
        "execution_admitted": False, "pit_verified": False, "limits": LIMITS + [
            "Readiness is for these annual inputs at actual retrieval time, not current tradable universe or complete QMJ",
            "Availability exclusions may overlap; source notice day is not assumed to be an exact release timestamp"]}
    write_json(root / "result.json", receipt)
    out = ROOT / "outputs/annual_financial_selection" / identifier
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", receipt | {"artifacts": str(root)})
    lines = ["# Annual Inputs Available As Of", "", "NO_TRADE. No signal or account replay.",
             f"As of {receipt['asof']}; annual period {period}; fields {','.join(fields)}.",
             f"Source rows {len(members)}; input-ready rows {len(selected)}; not received by then {receipt['not_received_by_asof_rows']}.",
             f"Unknown required fields {receipt['unknown_required_field_rows']}; unknown/later source notice days {receipt['publication_day_unknown_or_later_rows']}.",
             "", *["- " + value for value in receipt["limits"]], "", "Actual inputs: " + str(root / "available_annual_inputs.parquet")]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("id", "asof", "source_rows", "available_rows", "unknown_required_field_rows", "not_received_by_asof_rows")}), flush=True)
    return root, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, nargs="+")
    parser.add_argument("--primary-inputs", type=Path)
    parser.add_argument("--input-run", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--report-day")
    parser.add_argument("--fields", default="total_assets,operating_cashflow,revenue,operating_cost")
    args = parser.parse_args(argv)
    if args.input_run:
        if not args.as_of or not args.report_day or args.source_run or args.primary_inputs:
            parser.error("As-of reading needs input-run/as-of/report-day and no new source inputs")
        materialize_asof(args.input_run, args.report_day, args.as_of, args.fields.split(","))
        return 0
    if not args.source_run or args.as_of or args.report_day:
        parser.error("Build needs source-run, or use the explicit input-run/as-of/report-day mode")
    build(args.source_run, args.primary_inputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
