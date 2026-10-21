"""Fixed public-method-inspired profitability proxies through the existing ledger."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import END, START, bind_stock_inputs
from .low_risk import VARIANTS, portfolio, rank
from .market_replay import validate_prefix_inputs
from .report_period_calendar import POLICY as FIXED_PERIOD_POLICY, fixed_period, load_frozen_periods, period_observations
from .stock_defensive import stage_metrics
from .value_portfolios import ID as VALUE_ID, prepare_actions


ID = "economic_profitability_monthly_fixed_v1"
COHORT_ID = "economic_profitability_same_period_v1"
FIXED_PERIOD_ID = "economic_profitability_reporting_calendar_v1"
COHORT_POLICY = {
    "selection": "latest source_stat_date with at least portfolio.retain_rank observed members per saved anchor",
    "standardization": "recompute profitability ranks within the selected report-period cohort for all four cases",
    "minimum_members": "original fixed rank30 buffer; no return-based threshold selection",
    "annualization_or_ttm": False,
}
CASES = ("profit_matched_low_vol", "profit_matched_low_vol_half_momentum",
         "profitability_only", "risk_momentum_profitability")
FIELDS = ("roeAvg", "gpMargin")
FORMULAS = {
    "rank_z": "(average_tie_rank(x)-mean(rank(x)))/population_std(rank(x)); constant observed field has zero information",
    "profitability": "(rank_z(roeAvg_provider_value)+rank_z(gpMargin_provider_value))/2",
    "profitability_only": "profitability",
    "risk_momentum_profitability": "(rank_z(-volatility)+rank_z(momentum)+profitability)/3",
}
SOURCE = {"url": "https://images.aqr.com/-/media/AQR/Documents/Insights/Working-Papers/Quality-Minus-Junk.pdf",
          "read": "2013 working paper method section, pages9-11; Appendix A profitability definitions, page47",
          "lesson": "Monthly standardized ranks and fixed economic profitability directions; only two local provider proxies, not six-measure profitability or four-component QMJ replication"}
LIMITS = [
    "Partial acquired financial universe, NOT full-market quality evidence or an independently verified historical universe",
    "Provider quarterly average ROE/gross margin are adaptations, not original annual accounting definitions",
    "No assets/cashflow/accruals/growth/payout/industry-neutralization/size sorts/long-short QMJ replication",
    "Original positive-PB/nonzero-PE price pool reused; no new valuation score, future labels or action-availability filter",
    "Banks/other missing gross-margin observations excluded equally from all four new matched cases, never filled with zero",
    "Strict publication-date lag is NOT revision-safe PIT; source definitions/units are not globally verified",
    "Observed2025/2026 development, NOT untouched holdout, prospective results or parameter search",
    "New controls use the exact same acquired-financial pool, not the old broader value/control returns",
    "Existing sole Engine/Ledger owns cash/shares/costs/T1/lots/actions/limits; held unknown events stop that case",
    "No execution admission, high-Sharpe guarantee, black-swan protection or current account operation; NO_TRADE",
]


def rank_z(values):
    if not len(values) or not np.isfinite(values).all():
        raise ValueError("Rank standardization requires observed finite values; missing is not zero")
    ranks = values.rank(method="average")
    deviation = float(ranks.std(ddof=0))
    return ranks * 0 if deviation == 0 else (ranks - ranks.mean()) / deviation


def profitability_pool(pool, reports, day):
    """All cases receive the same contemporaneously known financial subset."""
    frame = pool.set_index("symbol").sort_index()
    if reports.symbol.duplicated().any() or frame.index.has_duplicates:
        raise ValueError("Distinct same-day financial and price securities required")
    columns = ["source_pub_date", "source_stat_date", *[field + "_provider_value" for field in FIELDS]]
    aligned = reports.set_index("symbol")[columns].reindex(frame.index)
    observed = aligned.source_pub_date.notna() & aligned.source_pub_date.ne("")
    if not aligned.loc[observed, "source_pub_date"].lt(day).all():
        raise ValueError("Same-day/future report cannot enter a profitability signal")
    finite = np.isfinite(aligned[[field + "_provider_value" for field in FIELDS]])
    if (finite.any(axis=1) & ~observed).any():
        raise ValueError("Financial value without publication provenance")
    available = finite.all(axis=1) & observed
    current = frame.loc[available].join(aligned.loc[available])
    if len(current):
        current["profitability"] = sum(rank_z(current[field + "_provider_value"]) for field in FIELDS) / len(FIELDS)
    audit = {"date": day, "original_price_pool": len(frame), "common_financial_pool": len(current),
             "coverage": len(current) / len(frame),
             **{field + "_observed": int(finite[field + "_provider_value"].sum()) for field in FIELDS}}
    return current, audit


def ranked_profitability(frame, case):
    if case not in CASES:
        raise ValueError("Unknown fixed profitability case")
    if case in CASES[:2]:
        return rank(frame.reset_index().to_dict("records"), VARIANTS[1] if case == CASES[0] else VARIANTS[2])
    current = frame.copy()
    current["score"] = (current.profitability if case == CASES[2] else
                        (rank_z(-current.volatility) + rank_z(current.momentum) + current.profitability) / 3)
    return current.reset_index().sort_values(["score", "symbol"], ascending=[False, True], kind="stable").to_dict("records")


def same_period_pool(pool, day, minimum_members, original_price_pool):
    """Select by dated report availability, never by outcomes or action coverage."""
    frame = pool.set_index("symbol").sort_index()
    if frame.index.has_duplicates or minimum_members < 1 or original_price_pool < len(frame):
        raise ValueError("Invalid saved financial cohort/counts")
    pub = pd.to_datetime(frame.source_pub_date, errors="raise")
    stat = pd.to_datetime(frame.source_stat_date, errors="raise")
    if (pub.isna().any() or stat.isna().any() or not pub.lt(pd.Timestamp(day)).all()
            or not stat.le(pub).all()):
        raise ValueError("Missing period or same-day/future financial provenance")
    if not np.isfinite(frame[[field + "_provider_value" for field in FIELDS]]).all().all():
        raise ValueError("Same-period cohort requires observed financial values")
    periods = stat.dt.strftime("%Y-%m-%d")
    counts = periods.value_counts()
    eligible = counts.loc[counts.ge(minimum_members)]
    if eligible.empty:
        raise ValueError(f"No report-period cohort meets fixed rank buffer: {day}")
    selected = max(eligible.index)
    current = frame.loc[periods.eq(selected)].copy()
    current["profitability"] = sum(rank_z(current[field + "_provider_value"]) for field in FIELDS) / len(FIELDS)
    return current, {"date": day, "original_price_pool": original_price_pool,
        "original_financial_pool": len(frame), "common_financial_pool": len(current),
        "selected_stat_date": selected, "excluded_period_members": len(frame) - len(current),
        "coverage": len(current) / original_price_pool,
        **{field + "_observed": len(current) for field in FIELDS}}


def cohort_coverage(pools, reports, minimum_members):
    """Measure the original cohort rule on a new view, without selecting a strategy."""
    if (minimum_members < 1 or not {"date", "symbol"} <= set(pools)
            or not {"date", "symbol"} <= set(reports)
            or pools.duplicated(["date", "symbol"]).any()
            or reports.duplicated(["date", "symbol"]).any()):
        raise ValueError("Distinct dated pools/reports and the original positive rank buffer required")
    columns = ["date", "symbol", "source_pub_date", "source_stat_date",
               *[field + "_provider_value" for field in FIELDS]]
    if not set(columns) <= set(reports):
        raise ValueError("Financial values and publication/report-period provenance required")
    audits, distributions, members = [], [], []
    for day, prices in pools[["date", "symbol"]].groupby("date", sort=True):
        frame, audit = profitability_pool(prices.drop(columns="date"), reports.loc[reports.date.eq(day)], day)
        pub = pd.to_datetime(frame.source_pub_date, errors="raise")
        stat = pd.to_datetime(frame.source_stat_date, errors="raise")
        if (pub.isna().any() or stat.isna().any() or not stat.le(pub).all()
                or not pub.lt(pd.Timestamp(day)).all()):
            raise ValueError("Joint observations require known chronological financial dates")
        counts = stat.dt.strftime("%Y-%m-%d").value_counts().sort_index()
        eligible = counts.loc[counts.ge(minimum_members)]
        selected = max(eligible.index) if len(eligible) else None
        audit.update(status="available_cohort" if selected else "insufficient_same_period_members",
                     latest_observed_stat_date=max(counts.index) if len(counts) else None,
                     latest_period_members=int(counts.iloc[-1]) if len(counts) else 0,
                     selected_stat_date=selected, selected_period_members=0, same_period_coverage=0.,
                     excluded_period_members=len(frame), minimum_members=minimum_members)
        if selected:
            current, selected_audit = same_period_pool(frame.reset_index(), day, minimum_members, len(prices))
            audit.update(selected_period_members=len(current), same_period_coverage=selected_audit["coverage"],
                         excluded_period_members=selected_audit["excluded_period_members"])
            # Export membership/provenance, not scores or an order-ready rank buffer.
            members.append(current.reset_index()[columns[1:]].assign(date=day)[columns])
        distributions.extend({"date": day, "source_stat_date": period, "members": int(count),
                              "meets_original_rank_buffer": bool(count >= minimum_members),
                              "selected": period == selected} for period, count in counts.items())
        audits.append(audit)
    if not audits:
        raise ValueError("At least one fixed decision pool required")
    membership = pd.concat(members, ignore_index=True) if members else pd.DataFrame(columns=columns)
    return pd.DataFrame(audits), pd.DataFrame(distributions, columns=[
        "date", "source_stat_date", "members", "meets_original_rank_buffer", "selected"]), membership


def coverage_run(identifier, financial_view):
    source_path = ROOT / "outputs/market_replay" / identifier / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("research_family") != VALUE_ID or source.get("status") != "completed_economic_value_diagnostics"
            or source.get("execution_admitted") is not False):
        raise ValueError("Completed unadmitted original value pool required; no changed account/ranks")
    pool_path = Path(source["rankings"]).parent / "monthly_value_pool.parquet"
    pools = pd.read_parquet(pool_path, columns=["date", "symbol"])
    expected = source["pool_summary"]
    if (len(pools) != expected["common_rows"] or pools.symbol.nunique() != expected["distinct_pool_symbols"]
            or pools.date.nunique() != expected["decision_months"]):
        raise ValueError("Original saved value pool counts changed")
    financial_view = Path(financial_view).resolve()
    financial = json.loads((financial_view / "result.json").read_text(encoding="utf-8"))
    if (financial.get("status") != "completed_publication_lagged_provider_view"
            or financial.get("execution_admitted") is not False):
        raise ValueError("Completed publication-lagged research view required")
    reports = pd.read_parquet(financial_view / "publication_lagged.parquet", columns=[
        "date", "symbol", "source_pub_date", "source_stat_date", *[field + "_provider_value" for field in FIELDS]])
    if len(reports) != financial["daily_rows"] or reports.symbol.nunique() != financial["symbols"]:
        raise ValueError("Financial view counts changed")
    reports["date"] = pd.to_datetime(reports.date).dt.strftime("%Y-%m-%d")
    minimum = source["protocol"]["portfolio"]["retain_rank"]
    audits, periods, membership = cohort_coverage(pools, reports, minimum)
    root = DATA_ROOT / "research/profitability_coverage" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, root / "implementation.py")
    audits.to_csv(root / "monthly_coverage.csv", index=False)
    periods.to_csv(root / "period_counts.csv", index=False)
    membership.to_parquet(root / "cohort_membership.parquet", index=False)
    result = {"status": "completed_same_period_financial_coverage", "source_run": str(source_path),
              "fixed_pool": str(pool_path), "financial_view": str(financial_view), "fields": list(FIELDS),
              "completed_symbol_histories": financial["symbols"], "declared_symbols": financial["declared_symbols"],
              "full_financial_universe_completed": financial["symbols"] == financial["declared_symbols"],
              "pool_rows": len(pools), "pool_symbols": int(pools.symbol.nunique()), "decision_months": len(audits),
              "minimum_members": minimum, "cohort_policy": COHORT_POLICY,
              "available_cohort_months": int(audits.status.eq("available_cohort").sum()),
              "mixed_period_months": int(periods.groupby("date").size().gt(1).sum()),
              "mean_monthly_joint_field_coverage": float(audits.coverage.mean()),
              "mean_monthly_same_period_coverage": float(audits.same_period_coverage.mean()),
              "min_same_period_members": int(audits.selected_period_members.min()),
              "max_same_period_members": int(audits.selected_period_members.max()),
              "same_period_rows": len(membership), "same_period_symbols": int(membership.symbol.nunique()),
              "rankings_changed": False, "account_replayed": False, "parameters_searched": False,
              "revision_safe_pit_verified": False, "strategy_validated": False, "execution_admitted": False,
              "limits": LIMITS + ["Coverage/membership only, NOT a completed broader matched ledger experiment",
                                  "Original rank30 cohort rule reused; no annualization, TTM, label or return selection",
                                  "Existing frozen775 financial strategy accounts/financial view remain unchanged"]}
    write_json(root / "result.json", result)
    print(json.dumps({"folder": str(root), **result}), flush=True)
    return root, result


def report(folder, record):
    title = ("# Fixed Profitability: Reporting-Calendar Cohort" if record["research_family"] == FIXED_PERIOD_ID else
             "# Fixed Profitability: Matched Same-Period Cohort" if record["research_family"] == COHORT_ID else
             "# Fixed Profitability: Matched Financial Subset")
    lines = [title, "", "Development diagnosis only; partial financial coverage. NO_TRADE.",
             f"{START}..{END}; 140000CNY; unchanged monthly20/rank30/costs and sole ledger.",
             "", "| Case | Status | Sessions | Net | MDD | Sharpe | Fees CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed | {row['completed_sessions']} | {row['net_return']:.2%} | "
                         f"{abs(row['max_drawdown']):.2%} | {row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} |")
        else:
            lines += [f"| {row['signal']} | failed | {row['completed_sessions']} | unknown | unknown | unknown | unknown |",
                      row.get("error", "Unknown input")]
    lines += ["", f"Pool: {record.get('pool_summary', 'not built')}", "", f"[Public method]({SOURCE['url']}): {SOURCE['lesson']}",
              "", "## Fixed Rules", ""] + [f"- {key}: {value}" for key, value in FORMULAS.items()]
    if record["protocol"].get("cohort_policy"):
        lines += ["", "## Report-Period Cohort", "", str(record["protocol"]["cohort_policy"]),
                  "[Official field documentation](https://www.baostock.com/mainContent?file=seasonProfit.md): "
                  "quarter-end/publication dates and average ROE defined; cumulative vs standalone not explicit. No TTM or annualization."]
        for item in record["protocol"]["cohort_policy"].get("sources", []):
            lines.append(f"- [Reporting-calendar source]({item['url']}): {item['read']}")
    lines += ["", "## Boundaries", ""] + ["- " + item for item in record["protocol"]["limits"]]
    if record.get("error"):
        lines += ["", record["error"]]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id=None, financial_view=None, *, same_period_id=None, resume_id=None,
        event_view=None, reference_view=None, refresh_prefixes=False, reporting_calendar=False):
    previous, cohort_source = None, None
    family = FIXED_PERIOD_ID if reporting_calendar else ID
    if resume_id:
        previous = json.loads((ROOT / "outputs/market_replay" / resume_id / "run.json").read_text(encoding="utf-8"))
        family = previous.get("research_family")
        if (family not in {ID, COHORT_ID, FIXED_PERIOD_ID} or previous["status"] not in {
                "completed_profitability_diagnostics", "failed_profitability_diagnostics"}
                or previous["protocol"]["formulas"] != FORMULAS or previous["protocol"]["signals"] != list(CASES)):
            raise ValueError("Terminal same fixed profitability experiment required")
        source_path = Path(previous["source_run"])
        financial_view = Path(previous["protocol"]["financial_view"])
        if family == COHORT_ID and previous["protocol"].get("cohort_policy") != COHORT_POLICY:
            raise ValueError("Resumed report-period policy changed")
        if family == FIXED_PERIOD_ID and previous["protocol"].get("cohort_policy") != FIXED_PERIOD_POLICY:
            raise ValueError("Resumed reporting-calendar policy changed")
    elif same_period_id:
        cohort_path = ROOT / "outputs/market_replay" / same_period_id / "run.json"
        cohort_source = json.loads(cohort_path.read_text(encoding="utf-8"))
        if (cohort_source.get("research_family") != ID or cohort_source["status"] != "completed_profitability_diagnostics"
                or cohort_source["protocol"]["formulas"] != FORMULAS
                or cohort_source["protocol"]["signals"] != list(CASES)):
            raise ValueError("Completed frozen original profitability experiment required")
        source_path = Path(cohort_source["source_run"])
        financial_view = Path(cohort_source["protocol"]["financial_view"])
        family = COHORT_ID
    else:
        source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
        financial_view = Path(financial_view).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    config = portfolio()
    if (source.get("research_family") != VALUE_ID or source["status"] != "completed_economic_value_diagnostics"
            or source["start"] != START or source["end"] != END or source["protocol"]["portfolio"] != config.to_dict()):
        raise ValueError("Completed original fixed valuation pool/config required")
    financial = json.loads((financial_view / "result.json").read_text(encoding="utf-8"))
    if (financial["status"] != "completed_publication_lagged_provider_view" or financial["execution_admitted"] is not False
            or sha256((financial_view / "publication_lagged.parquet").read_bytes()).hexdigest() != financial["panel_sha256"]):
        raise ValueError("Pinned unadmitted financial view required")
    if cohort_source and cohort_source["protocol"]["portfolio"] != config.to_dict():
        raise ValueError("Original financial cohort uses a different portfolio")
    bound = (previous or cohort_source or source)["protocol"]["stock_inputs"]
    inputs = bind_stock_inputs(bound, event_view, reference_view)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": family, "signals": list(CASES), "formulas": FORMULAS, "portfolio": config.to_dict(),
                "signal_portfolio_configs": {case: config.to_dict() for case in CASES}, "stock_inputs": inputs,
                "financial_view": str(financial_view), "financial_receipt": financial, "source": SOURCE,
                "common_pool": "Saved dated positive-PB/nonzero-PE price pool intersected with finite ROE and gross margin, equally for ALL4",
                "limits": LIMITS, "parameters_searched": False, "models_fitted": False,
                "fresh_holdout_claimed": False, "execution_admitted": False}
    if family == COHORT_ID:
        protocol.update(cohort_policy=COHORT_POLICY,
            cohort_source_run=(previous["protocol"]["cohort_source_run"] if previous else str(cohort_path.resolve())),
            provider_definition_boundary={"url": "https://www.baostock.com/mainContent?file=seasonProfit.md",
                "read": "Complete official seasonProfit.md on 2026-10-04",
                "known": "Average ROE formula, report/publication dates; netProfit and MBRevenue in CNY",
                "unknown": "Cumulative vs standalone quarterly values and revision-safe historical versions; no TTM/annualization"},
            common_pool="One latest report-period cohort per saved monthly anchor, shared by ALL4",
            limits=LIMITS + ["Same-period universe changes membership and may favor early reporters; not causal isolation of report period",
                             "No annualization/TTM; average ROE is not issuer weighted ROE; no global definition/PIT validation"])
    elif family == FIXED_PERIOD_ID:
        protocol.update(cohort_policy=FIXED_PERIOD_POLICY,
            common_pool="Exact calendar-chosen quarter from frozen raw receipts, actually disclosed before each anchor, equally for ALL4",
            limits=LIMITS + ["Reporting-calendar policy motivated by observed early-reporter diagnostics, NOT preregistered or causal isolation",
                             "Wider partial acquired scope is a NEW experiment; never a reproduction of frozen775 results",
                             "No fallback to early newer/late older reports, no annualization/TTM, no revised-PIT claim"])
    record = {"id": identifier, "status": "running", "research_family": family, "source_run": str(source_path.resolve()),
              "start": START, "end": END, "sessions": source["sessions"], "artifacts": str(artifacts),
              "protocol": protocol, "results": [], "comparisons": [], "execution_admitted": False,
              "purpose": family, "scope": ("available_financial_same_period_subset_profitability_research_only" if family in {COHORT_ID, FIXED_PERIOD_ID}
                                           else "available_financial_subset_profitability_research_only"), "dataset": inputs["price_view"],
              "strategy_validated": False, "started_at": datetime.now().astimezone().isoformat()}
    if previous:
        record["resume_run"] = resume_id
        record["refresh_incompatible_prefixes"] = refresh_prefixes
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, begin = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        dates = [day for day in stocks.calendar if START <= day <= END]
        if len(dates) != source["sessions"] or dates[0] != START or dates[-1] != END:
            raise ValueError("Exact original424-day account calendar required")
        protocol["stock_manifest"] = stocks.manifest
        record["symbols"] = len(stocks.symbols) - len(stocks.unsupported_instruments)
        if previous:
            if previous["protocol"]["financial_receipt"] != financial:
                raise ValueError("Resumed financial view changed")
            rankings = Path(previous["rankings"])
            record.update(rankings=str(rankings), pool_summary=previous["pool_summary"])
        else:
            original = cohort_source or source
            pool_path = Path(original["rankings"]).parent / ("monthly_profitability_pool.parquet" if cohort_source else "monthly_value_pool.parquet")
            price = pd.read_parquet(pool_path, columns=None if cohort_source else
                ["date", "symbol", "volatility", "momentum", "median_amount", "RSQR30", "WVMA20"])
            expected = original["pool_summary"]
            if (len(price) != expected["common_rows"] or price.symbol.nunique() != expected["distinct_pool_symbols"]
                    or price.date.nunique() != expected["decision_months"]):
                raise ValueError("Saved original price-pool count/identity changed")
            if cohort_source:
                original_counts = pd.read_csv(pool_path.parent / "pool_counts.csv").set_index("date")
                if original_counts.index.has_duplicates or set(original_counts.index) != set(price.date):
                    raise ValueError("Saved frozen financial-pool dates changed")
            elif family == FIXED_PERIOD_ID:
                reports = load_frozen_periods(financial_view, sorted(set(price.date)))
                reports.to_parquet(artifacts / "fixed_period_source_reports.parquet", index=False)
                shutil.copy2(Path(__file__).with_name("report_period_calendar.py"), artifacts / "report_period_calendar.py")
                protocol["fixed_period_source_reports"] = str(artifacts / "fixed_period_source_reports.parquet")
            else:
                reports = pd.read_parquet(financial_view / "publication_lagged.parquet", columns=[
                    "date", "symbol", "source_pub_date", "source_stat_date", *[field + "_provider_value" for field in FIELDS]])
                reports["date"] = pd.to_datetime(reports.date).dt.strftime("%Y-%m-%d")
            rankings, pools, audits = artifacts / "predictions", [], []
            ranking_rows = {case: [] for case in CASES}
            for day, original in price.groupby("date", sort=True):
                if cohort_source:
                    counts = original_counts.loc[day]
                    if len(original) != counts.common_financial_pool:
                        raise ValueError("Saved frozen financial membership count changed")
                    frame, audit = same_period_pool(original.drop(columns="date"), day, config.retain_rank, int(counts.original_price_pool))
                elif family == FIXED_PERIOD_ID:
                    frame, audit = profitability_pool(original.drop(columns="date"), period_observations(reports, day), day)
                    audit.update(selected_stat_date=fixed_period(day), period_policy="fixed_reporting_calendar_no_fallback")
                else:
                    frame, audit = profitability_pool(original.drop(columns="date"), reports.loc[reports.date.eq(day)], day)
                if len(frame) < config.retain_rank:
                    raise ValueError(f"Insufficient observed financial pool for fixed30 buffer: {day}")
                pools.append(frame.reset_index().assign(date=day))
                audits.append(audit)
                for case in CASES:
                    ranked = ranked_profitability(frame, case)
                    if len(ranked) < config.retain_rank:
                        raise ValueError(f"Insufficient fixed30 financial rank buffer: {day}: {case}")
                    ranking_rows[case].extend({"date": day, "rank": i, **row}
                                             for i, row in enumerate(ranked[:config.retain_rank], 1))
            panel = pd.concat(pools, ignore_index=True)
            panel.to_parquet(artifacts / "monthly_profitability_pool.parquet", index=False)
            pd.DataFrame(audits).to_csv(artifacts / "pool_counts.csv", index=False)
            record.update(rankings=str(rankings), pool_summary={"decision_months": len(audits), "common_rows": len(panel),
                "distinct_pool_symbols": int(panel.symbol.nunique()), "min_common_pool": min(row["common_financial_pool"] for row in audits),
                "max_common_pool": max(row["common_financial_pool"] for row in audits),
                "mean_monthly_coverage": sum(row["coverage"] for row in audits) / len(audits),
                "source_price_pool": str(pool_path), "full_financial_universe_completed": financial["symbols"] == financial["declared_symbols"]})
            for case in CASES:
                folder = rankings / case
                folder.mkdir(parents=True)
                pd.DataFrame(ranking_rows[case]).to_csv(folder / "rankings.csv", index=False)
            print(f"Fixed profitability subset: {record['pool_summary']}; no fit/grid or full-market claim", flush=True)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        for case in CASES:
            checkpoint = None
            if previous:
                prior = next(row for row in previous["results"] if row["signal"] == case)
                checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / case, config, dates)
                if len(checkpoint["daily"]) != prior["completed_sessions"]:
                    raise ValueError("Saved profitability case is not its terminal prefix")
                try:
                    ignored = validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                                     checkpoint["state"]["day"], checkpoint["state"])
                except ValueError as error:
                    if not refresh_prefixes:
                        raise
                    record.setdefault("explicit_historical_prefix_repairs", {})[case] = str(error)
                    checkpoint, ignored = None, []
                record.setdefault("uninvolved_source_changes", {})[case] = ignored or []
            feed = SavedRankFeed(stocks, rankings / case, family + ":" + case, config, dates)
            result = {"signal": case, **baseline_ledger_result(feed, config, dates, artifacts / case, checkpoint=checkpoint)}
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(artifacts / case, config.initial_cash)
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/{len(dates)} net={result.get('net_return')} {result.get('error', '')}", flush=True)
        by_case = {row["signal"]: row for row in record["results"]}
        for candidate in CASES[2:]:
            for control in CASES[:2]:
                left, right = by_case[candidate], by_case[control]
                if left["status"] == right["status"] == "completed":
                    record["comparisons"].append({"candidate": candidate, "control": control,
                        "net_difference": left["net_return"] - right["net_return"],
                        "drawdown_magnitude_difference": abs(left["max_drawdown"]) - abs(right["max_drawdown"])})
        record["status"] = ("completed_profitability_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "failed_profitability_diagnostics")
    except Exception as error:
        record.update(status="failed_profitability_diagnostics", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = time.monotonic() - begin
        write_json(out / "run.json", record)
        report(out, record)
        print(str(out), flush=True)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-run")
    group.add_argument("--same-period-run", help="Use a completed frozen profitability pool; all cases share one report-period cohort")
    group.add_argument("--resume-run")
    group.add_argument("--prepare-actions")
    group.add_argument("--coverage-run", help="Measure same-period membership on a new financial view; no ranks/accounts changed")
    parser.add_argument("--financial-view", type=Path)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--primary-view", type=Path)
    parser.add_argument("--max-fresh", type=int, default=64)
    parser.add_argument("--refresh-prefixes", action="store_true", help="Explicit historical restart of incompatible prefixes only; saved ranks/financial pool unchanged")
    parser.add_argument("--reporting-calendar", action="store_true", help="New fixed-quarter experiment from source-run and financial-view; no early-report fallback")
    args = parser.parse_args(argv)
    if args.reporting_calendar and not args.source_run:
        parser.error("Reporting-calendar creates a NEW source-run/financial-view experiment; resume retains its frozen policy")
    if args.coverage_run:
        if not args.financial_view or args.event_view or args.reference_view or args.primary_view or args.refresh_prefixes:
            parser.error("Cohort coverage needs the original value run and one financial view, not changed execution inputs")
        coverage_run(args.coverage_run, args.financial_view)
        return 0
    if args.prepare_actions:
        if not args.primary_view or args.financial_view or args.event_view or args.reference_view or args.refresh_prefixes:
            parser.error("Action preparation needs one primary-view and a terminal profitability run")
        prepare_actions(args.prepare_actions, args.primary_view, args.max_fresh, profitability=True)
        return 0
    if args.primary_view or args.refresh_prefixes and not args.resume_run or bool(args.source_run) != bool(args.financial_view):
        parser.error("Use source-run plus financial-view, same-period-run with its frozen pool, or resume-run with its frozen policy/view")
    result = run(args.source_run, args.financial_view, same_period_id=args.same_period_run, resume_id=args.resume_run,
                 event_view=args.event_view, reference_view=args.reference_view, refresh_prefixes=args.refresh_prefixes,
                 reporting_calendar=args.reporting_calendar)
    return 0 if result["status"] == "completed_profitability_diagnostics" else 1


if __name__ == "__main__":
    raise SystemExit(main())
