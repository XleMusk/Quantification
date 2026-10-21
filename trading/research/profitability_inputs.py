"""Bind frozen accounting inputs to existing profitability ranks, without refitting."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .financials import VALUES
from .profitability_portfolios import CASES, FIXED_PERIOD_ID, rank_z, ranked_profitability
from .report_period_calendar import fixed_period, load_frozen_periods


ID = "frozen_profitability_input_bridge_v1"
QARP_ID = "economic_qarp_reporting_calendar_v1"
QARP_CASES = ("qarp_only", "risk_momentum_qarp")
QARP_FORMULAS = {
    "value": "(rank_z(earnings_yield)+rank_z(book_yield))/2 in the SAME financial pool",
    "qarp_only": "(original two-field profitability + value)/2; fixed equal-family weights",
    "risk_momentum_qarp": "(rank_z(-volatility)+rank_z(momentum)+qarp_only)/3",
}
PROVENANCE = ["symbol", "source_pub_date", "source_stat_date"]
PROXIES = ["roeAvg_provider_value", "gpMargin_provider_value"]
SOURCES = [
    {"url": "https://www.baostock.com/mainContent?file=seasonProfit.md",
     "read": "Full official field page, via its public /helpdocs/api/markdown/seasonProfit.md POST route; no market-data query/login",
     "finding": "Rate labels/formulas use percent, while the official example uses .074617/.342179. No standalone-vs-YTD specification; do not silently convert"},
    {"url": "https://images.aqr.com/-/media/AQR/Documents/Insights/Working-Papers/Quality-Minus-Junk.pdf",
     "read": "2013 method pages9-11 and Appendix A profitability definitions, page47",
     "finding": "Six accounting profitability measures, normally annual, are distinct from this two-provider-field proxy"},
]
LIMITS = [
    "Frozen acquired-financial subset, not all historical A-shares or independently verified PIT",
    "Publication strictly before decision; report vintages/revisions and standalone vs YTD remain unverified",
    "NetProfit/MBRevenue arithmetic is local evidence only; main-business revenue may differ from the documented margin denominator",
    "Neither arithmetic hypothesis matching is not proof of erroneous data; both matching is ambiguous, not a verified unit",
    "No ROE denominator inferred from consolidated net profit; no invented assets, cashflows, accruals or TTM",
    "Uniform positive unit rescaling does not create new ranking information; old ranks/accounts remain unchanged",
    "First ranked candidates are not retained target holdings, actual fills or a causal return attribution",
    "Observed2025/2026 development; no return labels, threshold/weight search, new ledger or execution admission. NO_TRADE",
]


def bind_inputs(pool, reports):
    required = {"date", *PROVENANCE, *PROXIES, "profitability", "volatility", "momentum"}
    if (not required <= set(pool) or not {"symbol", "pubDate", "statDate", *VALUES} <= set(reports)
            or pool.duplicated(["date", "symbol"]).any() or reports.duplicated(["symbol", "statDate"]).any()):
        raise ValueError("Distinct frozen pools and exact quarterly accounting observations required")
    pub, stat = pd.to_datetime(reports.pubDate, errors="raise"), pd.to_datetime(reports.statDate, errors="raise")
    if pub.isna().any() or stat.isna().any() or not stat.le(pub).all():
        raise ValueError("Known chronological report dates required")
    expected = pool.date.map(fixed_period)
    if (pool[sorted(required - {"volatility", "momentum"})].isna().any().any()
            or not pool.source_stat_date.eq(expected).all() or not pool.source_pub_date.lt(pool.date).all()):
        raise ValueError("Original reporting-calendar pool with strictly earlier publication required")
    raw = reports.rename(columns={"pubDate": "source_pub_date", "statDate": "source_stat_date",
                                  **{field: field + "_provider_value" for field in VALUES}})
    joined = pool.merge(raw[PROVENANCE + [field + "_provider_value" for field in VALUES]],
                        on=PROVENANCE, how="left", validate="many_to_one", suffixes=("", "_raw"), indicator=True)
    if not joined._merge.eq("both").all():
        raise ValueError("Saved pool report not present in its frozen quarterly receipts")
    for field in PROXIES:
        if not np.array_equal(joined[field].to_numpy(), joined[field + "_raw"].to_numpy()):
            raise ValueError("Original financial proxy changed")
    joined = joined.drop(columns=["_merge", *[field + "_raw" for field in PROXIES]])
    frames = []
    for _, frame in joined.groupby("date", sort=True):
        frame = frame.sort_values("symbol").copy()
        frame["roe_rank_z"] = rank_z(frame.roeAvg_provider_value)
        frame["gross_margin_rank_z"] = rank_z(frame.gpMargin_provider_value)
        score = (frame.roe_rank_z + frame.gross_margin_rank_z) / 2
        if not np.allclose(score, frame.profitability, rtol=0, atol=1e-12):
            raise ValueError("Frozen profitability score cannot be reproduced")
        scaled = sum(rank_z(frame[field] * 100) for field in PROXIES) / 2
        if not np.array_equal(score.to_numpy(), scaled.to_numpy()):
            raise ValueError("Uniform unit rescaling changed ranks; investigate representation/ties")
        frames.append(frame)
    if not frames:
        raise ValueError("At least one frozen financial decision pool required")
    return pd.concat(frames, ignore_index=True)


def margin_evidence(reports):
    frame = reports[["symbol", "pubDate", "statDate", "npMargin", "netProfit", "MBRevenue"]].copy()
    observed = np.isfinite(frame[["npMargin", "netProfit", "MBRevenue"]]).all(axis=1) & frame.MBRevenue.gt(0)
    expected = (frame.netProfit / frame.MBRevenue.where(observed)).where(observed)
    frame["net_profit_over_main_revenue"] = expected
    frame["ratio_error"] = (frame.npMargin - expected).abs()
    frame["percent_error"] = (frame.npMargin / 100 - expected).abs()
    # Provider ratios have six decimal places; compare arithmetic, not investment outcomes.
    ratio = observed & np.isclose(frame.npMargin, expected, rtol=1e-4, atol=1e-6)
    percent = observed & np.isclose(frame.npMargin / 100, expected, rtol=1e-4, atol=1e-6)
    frame["arithmetic_status"] = np.select(
        [ratio & ~percent, percent & ~ratio, ratio & percent, observed],
        ["ratio_supported", "percent_supported", "ambiguous_both", "neither_supported"],
        default="unknown_inputs_or_nonpositive_revenue")
    return frame


def profile(frame, day, group):
    row = {"date": day, "group": group, "members": len(frame)}
    for field in VALUES:
        values = frame[field + "_provider_value"]
        observed = values[np.isfinite(values)]
        row[field + "_observed"] = len(observed)
        row[field + "_negative"] = int(observed.lt(0).sum())
        row[field + "_median_raw"] = float(observed.median()) if len(observed) else None
    row["negative_momentum"] = int(frame.momentum.le(0).sum())
    row["negative_roe_and_positive_gross_margin"] = int(
        (frame.roeAvg_provider_value.lt(0) & frame.gpMargin_provider_value.gt(0)).sum())
    row["below_pool_mean_roe_rank"] = int(frame.roe_rank_z.lt(0).sum())
    row["roe_gross_margin_rank_correlation"] = (float(frame.roe_rank_z.corr(frame.gross_margin_rank_z))
        if frame.roe_rank_z.nunique() > 1 and frame.gross_margin_rank_z.nunique() > 1 else None)
    return row


def rank_profiles(features, rankings, holdings, retain_rank):
    if holdings < 1 or retain_rank < holdings or set(rankings) != set(CASES):
        raise ValueError("Original fixed candidate counts and all matched cases required")
    profiles, candidates = [], []
    for case in CASES:
        saved = rankings[case]
        if (saved.duplicated(["date", "symbol"]).any() or saved.duplicated(["date", "rank"]).any()
                or set(saved.date) != set(features.date)):
            raise ValueError("Distinct saved ranks on identical frozen decision dates required")
        for day, frame in features.groupby("date", sort=True):
            current = saved.loc[saved.date.eq(day)].sort_values("rank")
            expected = ranked_profitability(frame.set_index("symbol"), case)[:retain_rank]
            if (current["rank"].tolist() != list(range(1, retain_rank + 1))
                    or current.symbol.tolist() != [r["symbol"] for r in expected]
                    or not np.allclose(current.score, [r["score"] for r in expected], rtol=0, atol=1e-12)):
                raise ValueError("Saved candidate ordering disagrees with original frozen scores")
            members = current[["date", "symbol", "rank", "score"]].merge(
                frame.drop(columns="score", errors="ignore"), on=["date", "symbol"], validate="one_to_one")
            members["case"] = case
            members["roe_score_component"] = members.roe_rank_z / 2
            members["gross_margin_score_component"] = members.gross_margin_rank_z / 2
            candidates.append(members)
            profiles.append(profile(members.loc[members["rank"].le(holdings)], day, case + "_head"))
            if case == CASES[0]:
                profiles.append(profile(frame, day, "common_pool"))
    return pd.DataFrame(profiles), pd.concat(candidates, ignore_index=True)


def specifications():
    formulas = {"GPOA": "(revenue - cost_of_goods_sold) / total_assets",
                "ROE": "annual_net_income / book_equity",
                "ROA": "annual_net_income / total_assets",
                "CFOA": "(net_income + depreciation - working_capital_change - capital_expenditure) / total_assets",
                "GMAR": "(revenue - cost_of_goods_sold) / sales",
                "ACC": "(depreciation - working_capital_change) / total_assets"}
    gaps = {"GPOA": ["annual revenue/COGS", "total assets"],
            "ROE": ["annual income/book equity", "average-equity vs book-equity definition", "historical revision versions"],
            "ROA": ["annual income", "total assets"],
            "CFOA": ["annual cashflow constituents", "total assets"],
            "GMAR": ["annual revenue/COGS/sales definition", "rate scale clarification", "historical revision versions"],
            "ACC": ["depreciation/working capital changes", "total assets"]}
    return {"id": ID, "sources": SOURCES,
        "usable_inputs": {field: {"column": field + "_provider_value", "status": "provider_observation_only",
            "unit": "official description CNY" if field in {"netProfit", "MBRevenue"} else
                    "official definition TTM earnings per latest share" if field == "epsTTM" else
                    "raw share count; global reconciliation unknown" if field in {"totalShare", "liqaShare"} else
                    "rate scale unresolved; sign/rank only",
            "period": "provider quarter-end, flow standalone-vs-YTD unknown" if field != "epsTTM" else
                      "provider-labelled TTM, vintage/revision unverified"} for field in VALUES},
        "public_profitability_measures": {key: {"formula": formula, "status": "blocked_exact_reproduction",
            "missing_or_unverified": gaps[key]} for key, formula in formulas.items()},
        "existing_proxy": {"fields": PROXIES, "annual_accounting_equivalence": False,
            "uniform_positive_scale_changes_rank": False, "units_converted": False,
        "missing_values_imputed": False}, "execution_admitted": False}


def bind_valuation(features, value_pool):
    fields = ["peTTM", "pbMRQ", "earnings_yield", "book_yield"]
    if (not {"date", "symbol", *fields, "momentum", "volatility"} <= set(value_pool)
            or value_pool.duplicated(["date", "symbol"]).any() or features.duplicated(["date", "symbol"]).any()):
        raise ValueError("Distinct original dated valuation pool required")
    joined = features.merge(value_pool[["date", "symbol", "momentum", "volatility", *fields]],
        on=["date", "symbol"], how="left", validate="one_to_one", suffixes=("", "_value"), indicator=True)
    if (not joined._merge.eq("both").all() or not np.isfinite(joined[fields]).all().all()
            or not joined.pbMRQ.gt(0).all() or not joined.peTTM.ne(0).all()):
        raise ValueError("Original price-pool valuation observations unavailable; no imputation")
    for field in ("momentum", "volatility"):
        if not np.array_equal(joined[field].to_numpy(), joined[field + "_value"].to_numpy()):
            raise ValueError("Valuation source and financial price features differ")
    if (not np.allclose(joined.earnings_yield, 1 / joined.peTTM, rtol=1e-12, atol=0)
            or not np.allclose(joined.book_yield, 1 / joined.pbMRQ, rtol=1e-12, atol=0)):
        raise ValueError("Original valuation definitions changed")
    joined = joined.drop(columns=["_merge", "momentum_value", "volatility_value"])
    frames = []
    for _, frame in joined.groupby("date", sort=True):
        frame = frame.sort_values("symbol").copy()
        for field in ("earnings_yield", "book_yield"):
            frame[field + "_rank_z"] = rank_z(frame[field])
            frame[field + "_rank_percentile"] = frame[field].rank(method="average", pct=True)
        frame["value_score"] = (frame.earnings_yield_rank_z + frame.book_yield_rank_z) / 2
        frame["qarp_score"] = (frame.profitability + frame.value_score) / 2
        frames.append(frame)
    if not frames:
        raise ValueError("Observed frozen decision pools required")
    return pd.concat(frames, ignore_index=True)


def valuation_profile(frame, day, group):
    return {"date": day, "group": group, "members": len(frame),
        "median_pe_raw": float(frame.peTTM.median()), "median_pb_raw": float(frame.pbMRQ.median()),
        "negative_pe_members": int(frame.peTTM.lt(0).sum()),
        "mean_earnings_yield_rank_percentile": float(frame.earnings_yield_rank_percentile.mean()),
        "mean_book_yield_rank_percentile": float(frame.book_yield_rank_percentile.mean()),
        "below_pool_median_book_yield": int(frame.book_yield_rank_percentile.lt(.5).sum()),
        "profitability_earnings_yield_spearman": float(frame.profitability.corr(frame.earnings_yield, method="spearman")),
        "profitability_book_yield_spearman": float(frame.profitability.corr(frame.book_yield, method="spearman"))}


def qarp_ranks(frame, case, retain_rank):
    if case not in QARP_CASES or len(frame) < retain_rank or retain_rank < 1:
        raise ValueError("Fixed QARP case and sufficient unchanged common pool required")
    current = frame.copy()
    current["score"] = (current.qarp_score if case == QARP_CASES[0] else
        (rank_z(-current.volatility) + rank_z(current.momentum) + current.qarp_score) / 3)
    current = current.sort_values(["score", "symbol"], ascending=[False, True], kind="stable").iloc[:retain_rank].copy()
    current["rank"] = range(1, retain_rank + 1)
    return current


def valuation_run(inputs_id):
    from .value_portfolios import ID as VALUE_ID

    inputs_path = ROOT / "outputs/profitability_inputs" / inputs_id / "result.json"
    receipt = json.loads(inputs_path.read_text(encoding="utf-8"))
    if (receipt.get("id") != ID or receipt.get("status") != "completed_frozen_profitability_inputs"
            or receipt.get("execution_admitted") is not False):
        raise ValueError("Completed frozen accounting input bridge required")
    source = json.loads(Path(receipt["source_run"]).read_text(encoding="utf-8"))
    value_path = Path(source["source_run"])
    value_source = json.loads(value_path.read_text(encoding="utf-8"))
    if (source.get("research_family") != FIXED_PERIOD_ID or source.get("execution_admitted") is not False
            or value_source.get("research_family") != VALUE_ID
            or value_source.get("status") != "completed_economic_value_diagnostics"
            or value_source.get("execution_admitted") is not False):
        raise ValueError("Original fixed financial and value sources required")
    features = pd.read_parquet(Path(receipt["artifacts"]) / "monthly_feature_inputs.parquet")
    candidates = pd.read_parquet(Path(receipt["artifacts"]) / "saved_candidate_components.parquet")
    if (len(features) != receipt["pool_rows"] or features.symbol.nunique() != receipt["pool_symbols"]
            or features.date.nunique() != receipt["decision_months"]
            or len(candidates) != receipt["saved_rank_rows_verified"]):
        raise ValueError("Frozen feature/candidate scope changed")
    values = pd.read_parquet(Path(value_source["rankings"]).parent / "monthly_value_pool.parquet")
    if len(values) != value_source["pool_summary"]["common_rows"]:
        raise ValueError("Original valuation source scope changed")
    joined = bind_valuation(features, values)
    name = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder, data = ROOT / "outputs/profitability_inputs" / name, DATA_ROOT / "research/profitability_inputs" / name
    folder.mkdir(parents=True, exist_ok=False)
    data.mkdir(parents=True, exist_ok=False)
    profiles = []
    config = source["protocol"]["portfolio"]
    for day, frame in joined.groupby("date", sort=True):
        profiles.append(valuation_profile(frame, day, "common_pool"))
        for case in CASES:
            head = candidates.loc[candidates.date.eq(day) & candidates.case.eq(case)
                                  & candidates["rank"].le(config["holdings"])]
            members = frame.loc[frame.symbol.isin(head.symbol)]
            if len(head) != config["holdings"] or len(members) != len(head):
                raise ValueError("Original complete matched candidate heads required")
            profiles.append(valuation_profile(members, day, case + "_head"))
    profiles = pd.DataFrame(profiles)
    joined.to_parquet(data / "monthly_feature_inputs.parquet", index=False)
    profiles.to_csv(data / "monthly_valuation_profiles.csv", index=False)
    rankings = data / "predictions"
    for case in QARP_CASES:
        target = rankings / case
        target.mkdir(parents=True, exist_ok=False)
        pd.concat([qarp_ranks(frame, case, config["retain_rank"]) for _, frame in joined.groupby("date", sort=True)],
                  ignore_index=True).to_csv(target / "rankings.csv", index=False)
    summaries = [{"group": group, "candidate_slots": int(frame.members.sum()),
        "mean_monthly_median_pb": float(frame.median_pb_raw.mean()),
        "negative_pe_slots": int(frame.negative_pe_members.sum()),
        "mean_earnings_yield_rank_percentile": float(frame.mean_earnings_yield_rank_percentile.mean()),
        "mean_book_yield_rank_percentile": float(frame.mean_book_yield_rank_percentile.mean()),
        "below_pool_median_book_yield_slots": int(frame.below_pool_median_book_yield.sum())}
        for group, frame in profiles.groupby("group", sort=True)]
    result = {"status": "completed_frozen_profitability_valuation", "inputs_run": str(inputs_path),
        "source_run": receipt["source_run"], "value_source_run": str(value_path), "artifacts": str(data),
        "pool_rows": len(joined), "pool_symbols": int(joined.symbol.nunique()),
        "decision_months": int(joined.date.nunique()), "candidate_profiles": summaries,
        "rankings": str(rankings), "signal_specification": {"id": QARP_ID, "signals": list(QARP_CASES),
            "formulas": QARP_FORMULAS, "portfolio": config,
            "boundary": "Method-inspired two-field profitability/value adaptation, NOT QMJ/QARP paper replication"},
        "sources": SOURCES + [{"url": SOURCES[1]["url"], "read": "2013 QARP discussion page6",
            "finding": "Pricing and quality are distinct; equal-family local formula fixed without looking at new account outcomes"}],
        "rankings_changed": False, "new_candidate_rankings_created": True, "account_replayed": False,
        "returns_evaluated": False, "parameters_searched": False, "strategy_validated": False,
        "execution_admitted": False, "limits": LIMITS + [
            "Median of each month's candidates averaged equally; not a pooled/holding-weighted PB or causal overpricing proof",
            "Provider PE/PB and quarterly profitability differ in denominator/time horizon; valuation PIT not independently verified",
            "New fixed equal-family ranks are inputs for the sole ledger; no completed account result or readiness claim"]}
    shutil.copy2(__file__, data / "implementation.py")
    write_json(folder / "result.json", result)
    lines = ["# Saved Profitability: Valuation And Fixed QARP Inputs", "", "NO_TRADE. Original ranks/accounts unchanged.",
        "Frozen accounting bridge reused; no quarterly reread or data-service query.",
        f"{len(joined)} members / {result['pool_symbols']} securities / {result['decision_months']} months, same common pool.",
        "", "| Group | Slots | Mean Monthly Median PB | Negative PE Slots | Mean E/P Percentile | Mean B/P Percentile | Below Pool Median B/P |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    lines += [f"| {r['group']} | {r['candidate_slots']} | {r['mean_monthly_median_pb']:.4f} | {r['negative_pe_slots']} | "
        f"{r['mean_earnings_yield_rank_percentile']:.2%} | {r['mean_book_yield_rank_percentile']:.2%} | "
        f"{r['below_pool_median_book_yield_slots']} |" for r in summaries]
    lines += ["", "## Next Fixed Candidate Inputs", ""] + [f"- {key}: {formula}" for key, formula in QARP_FORMULAS.items()]
    lines += ["", "Generated top30 ranks for two cases, but NO account replay/return evaluation yet.",
        f"Data and ranks: {data}", "", "## Sources", ""] + [f"- [{s['read']}]({s['url']}): {s['finding']}" for s in result["sources"]]
    lines += ["", "## Boundaries", ""] + ["- " + s for s in result["limits"]]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder, result


def run(identifier):
    source_path = ROOT / "outputs/market_replay" / identifier / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("research_family") != FIXED_PERIOD_ID
            or source.get("status") != "completed_profitability_diagnostics"
            or source.get("execution_admitted") is not False):
        raise ValueError("Completed unadmitted reporting-calendar source required")
    ranks = Path(source["rankings"])
    pools = pd.read_parquet(ranks.parent / "monthly_profitability_pool.parquet")
    counts = source["pool_summary"]
    if (len(pools) != counts["common_rows"] or pools.symbol.nunique() != counts["distinct_pool_symbols"]
            or pools.date.nunique() != counts["decision_months"]):
        raise ValueError("Frozen common financial pool changed")
    name = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder, data = ROOT / "outputs/profitability_inputs" / name, DATA_ROOT / "research/profitability_inputs" / name
    folder.mkdir(parents=True, exist_ok=False)
    data.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "id": ID, "source_run": str(source_path), "artifacts": str(data),
        "sources": SOURCES, "limits": LIMITS, "rankings_changed": False, "account_replayed": False,
        "strategy_validated": False, "execution_admitted": False}
    write_json(folder / "result.json", result)
    shutil.copy2(__file__, data / "implementation.py")
    try:
        print("Reading exact frozen quarterly fields for existing decision periods; no provider login/query", flush=True)
        reports = load_frozen_periods(source["protocol"]["financial_view"], sorted(pools.date.unique()))
        features = bind_inputs(pools, reports)
        evidence = margin_evidence(reports)
        portfolio = source["protocol"]["portfolio"]
        saved = {case: pd.read_csv(ranks / case / "rankings.csv", dtype={"date": str, "symbol": str}) for case in CASES}
        profiles, candidates = rank_profiles(features, saved, portfolio["holdings"], portfolio["retain_rank"])
        reports.to_parquet(data / "frozen_quarterly_inputs.parquet", index=False)
        features.to_parquet(data / "monthly_feature_inputs.parquet", index=False)
        evidence.to_csv(data / "margin_arithmetic.csv", index=False)
        profiles.to_csv(data / "monthly_profiles.csv", index=False)
        candidates.to_parquet(data / "saved_candidate_components.parquet", index=False)
        write_json(data / "factor_specifications.json", specifications())
        summaries = []
        for group, rows in profiles.groupby("group", sort=True):
            summary = {"group": group, "candidate_slots": int(rows.members.sum()),
                "negative_roe_slots": int(rows.roeAvg_negative.sum()),
                "negative_net_profit_slots": int(rows.netProfit_negative.sum()),
                "unknown_net_profit_slots": int((rows.members - rows.netProfit_observed).sum()),
                "nonpositive_momentum_slots": int(rows.negative_momentum.sum()),
                "below_pool_mean_roe_rank_slots": int(rows.below_pool_mean_roe_rank.sum())}
            summaries.append(summary)
        result.update(status="completed_frozen_profitability_inputs", pool_rows=len(features),
            pool_symbols=int(features.symbol.nunique()), decision_months=int(features.date.nunique()),
            exact_frozen_report_rows=len(reports), exact_frozen_periods=sorted(reports.statDate.unique().tolist()),
            reports_by_field={field: int(np.isfinite(reports[field]).sum()) for field in VALUES},
            margin_arithmetic=evidence.arithmetic_status.value_counts().to_dict(),
            saved_rank_rows_verified=len(candidates), unit_rescaling_score_changes=0,
            candidate_profiles=summaries, exact_public_measures_available=0,
            cumulative_to_single_quarter_or_ttm=False, unit_normalization_globally_verified=False)
    except Exception as exc:
        result.update(status="failed_frozen_profitability_inputs", error=f"{type(exc).__name__}: {exc}")
        write_json(folder / "result.json", result)
        raise
    write_json(folder / "result.json", result)
    lines = ["# Frozen Profitability Input Bridge", "", "NO_TRADE. No account replay or rank/weight refit.",
        f"{result['pool_rows']} dated members / {result['pool_symbols']} securities / {result['decision_months']} months.",
        f"{len(reports)} exact quarterly observations, all eight fields bound to the original pool.",
        "", "## Accounting Arithmetic", "", str(result["margin_arithmetic"]),
        "", "Official labels say percent, but the example uses ratio-like numbers. Arithmetic evidence is local only.",
        "No missing revenue/asset denominator is inferred and no quarterly flow is converted to TTM.",
        "", "## Saved Ranked Candidates", "",
        "| Group | Slots | Negative ROE | Negative NP | Unknown NP | Nonpositive Momentum | Below Pool Mean ROE Rank |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    lines += [f"| {r['group']} | {r['candidate_slots']} | {r['negative_roe_slots']} | {r['negative_net_profit_slots']} | "
        f"{r['unknown_net_profit_slots']} | {r['nonpositive_momentum_slots']} | {r['below_pool_mean_roe_rank_slots']} |"
        for r in summaries]
    lines += ["", "All saved matched rank buffers reproduced; uniform x100 units change zero scores.",
        "The six public annual profitability measures remain blocked as exact reproductions, not zero-valued factors.",
        "The feature table and score components are reusable research inputs, not submitted orders or realized returns.",
        "", "## Sources", ""] + [f"- [{s['read']}]({s['url']}): {s['finding']}" for s in SOURCES]
    lines += ["", "## Data", "", str(data), "", "## Boundaries", ""] + ["- " + s for s in LIMITS]
    (folder / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-run")
    group.add_argument("--inputs-run", help="Reuse an existing bridge for valuation exposure and fixed QARP ranks")
    args = parser.parse_args(argv)
    folder, result = valuation_run(args.inputs_run) if args.inputs_run else run(args.source_run)
    print(json.dumps({"folder": str(folder), **result}, ensure_ascii=False))
    return 0
