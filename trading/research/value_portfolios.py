"""Fixed economic value scores and newly matched controls through the sole ledger."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed, numeric
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import END, START, bind_stock_inputs, common_pool, read_native_monthends
from .financials import VALUES as FINANCIAL_FIELDS
from .low_risk import LowRiskFeed, VARIANTS, features, portfolio, rank
from .market_replay import validate_prefix_inputs
from .monthly_timing import month_ends
from .stock_defensive import stage_metrics


ID = "economic_value_monthly_fixed_v1"
CASES = ("matched_low_vol", "matched_low_vol_half_momentum", "ep_bp_value", "risk_momentum_value")
CONTROLS = CASES[:2]
FORMULAS = {
    "earnings_yield": "1/raw_peTTM, including negative values; zero/missing PE unknown",
    "book_yield": "1/raw_pbMRQ with positive PB; nonpositive/missing PB ineligible for ALL cases",
    "value_rank": "(rank_pct(earnings_yield)+rank_pct(book_yield))/2",
    "ep_bp_value": "value_rank",
    "risk_momentum_value": "(rank_pct(-volatility)+rank_pct(momentum)+value_rank)/3",
}
SOURCES = [
    {"url": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_port_form_ep.html",
     "read": "construction details", "lesson": "Earnings/price sorting distinguishes negative earnings; local signed TTM yield is a declared adaptation, not the original annual portfolios"},
    {"url": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/six_portfolios.html",
     "read": "construction details", "lesson": "Book/market with positive book equity; local reciprocal provider PB is a proxy, not original fiscal-year and size-breakpoint construction"},
    {"url": "https://images.aqr.com/-/media/AQR/Documents/Insights/Working-Papers/Quality-Minus-Junk.pdf",
     "read": "method sections 1 and portfolio construction, pages8-11, not full-paper review",
     "lesson": "Profitability, growth, safety and payout are distinct quality components; price multiples alone do not establish QMJ or quality"},
]
LIMITS = [
    "Inspected2025/2026 development evidence, NOT untouched out-of-sample/prospective or execution admission; NO_TRADE",
    "Directions are fixed economic priors, high signed E/P and positive B/P, NOT fitted or chosen from2026 returns; no training/grid",
    "Retrieved historical PE/PB may be revised, with no independent publication-time/PIT verification",
    "Provider reciprocal ratios are valuation proxies, not validated fiscal-year E/P or B/M with original paper timing/share definitions",
    "Negative earnings remain negative E/P and are not called cheap; zero PE is unknown, not zero earnings",
    "All four cases share contemporaneous finite PE/PB/native/price inputs; no future labels or action availability filter the pool",
    "Two controls must be newly replayed on this common pool, not borrowed from older broader-universe accounts",
    "No industry/cap neutralization, QMJ/operating-profitability replication, ETF/BSE completeness or tail protection claim",
    "Quarterly ROE coverage is diagnostic only; limited financial membership does not restrict any value strategy",
    "Existing Engine/Ledger owns stocks/cash/costs/T1/lots/actions/limits; unresolved held events stop the affected account",
]


def value_pool(pool, raw):
    frame = pd.DataFrame(pool).set_index("symbol").sort_index()
    fields = pd.DataFrame.from_dict(raw, orient="index").reindex(frame.index)
    if fields.empty or not {"peTTM", "pbMRQ"} <= set(fields):
        raise ValueError("Dated provider valuation fields required")
    for key in ("peTTM", "pbMRQ"):
        frame[key] = numeric(fields[key])
    pe_known, pb_known = np.isfinite(frame.peTTM), np.isfinite(frame.pbMRQ)
    available = pe_known & frame.peTTM.ne(0) & pb_known & frame.pbMRQ.gt(0)
    audit = {"original_common_pool": len(frame), "pe_missing": int((~pe_known).sum()),
             "pe_zero_unknown": int((pe_known & frame.peTTM.eq(0)).sum()),
             "negative_pe_observed": int((pe_known & frame.peTTM.lt(0)).sum()),
             "pb_missing": int((~pb_known).sum()), "nonpositive_pb": int((pb_known & frame.pbMRQ.le(0)).sum()),
             "value_common_pool": int(available.sum())}
    current = frame.loc[available].copy()
    current["earnings_yield"] = 1 / current.peTTM
    current["book_yield"] = 1 / current.pbMRQ
    if not np.isfinite(current[["earnings_yield", "book_yield"]].to_numpy()).all():
        raise ValueError("Reciprocal valuation overflow, do not silently clip")
    current["value_rank"] = (current.earnings_yield.rank(pct=True) + current.book_yield.rank(pct=True)) / 2
    return current, audit


def ranked_value(frame, case):
    if case not in CASES:
        raise ValueError("Unknown fixed economic-value case")
    if case in CONTROLS:
        return rank(frame.reset_index().to_dict("records"), VARIANTS[1] if case == CONTROLS[0] else VARIANTS[2])
    result = frame.copy()
    result["score"] = (result.value_rank if case == "ep_bp_value" else
                       ((-result.volatility).rank(pct=True) + result.momentum.rank(pct=True) + result.value_rank) / 3)
    return result.reset_index().sort_values(["score", "symbol"], ascending=[False, True], kind="stable").to_dict("records")


def financial_coverage(view, pools):
    """Measure financial-field availability without shrinking the saved strategy pool."""
    view = Path(view).resolve()
    receipt = json.loads((view / "result.json").read_text(encoding="utf-8"))
    if receipt["status"] != "completed_publication_lagged_provider_view" or receipt["execution_admitted"] is not False:
        raise ValueError("Existing unadmitted publication-lagged report view required")
    source = pd.read_parquet(view / "publication_lagged.parquet", columns=[
        "date", "symbol", "source_pub_date", "source_stat_date",
        *[field + "_provider_value" for field in FINANCIAL_FIELDS]])
    source["date"] = pd.to_datetime(source.date).dt.strftime("%Y-%m-%d")
    if source.duplicated(["date", "symbol"]).any():
        raise ValueError("Distinct report-panel date/security rows required")
    observed = source.source_pub_date.ne("")
    if not source.loc[observed, "source_pub_date"].lt(source.loc[observed, "date"]).all():
        raise ValueError("Same-day/future report publication cannot enter coverage")
    merged = pools[["date", "symbol"]].merge(source, on=["date", "symbol"], how="left", validate="one_to_one")
    for field in FINANCIAL_FIELDS:
        merged[field + "_observed"] = np.isfinite(merged[field + "_provider_value"])
    profitability_fields = ("roeAvg", "npMargin", "gpMargin")
    merged["profitability_observed"] = merged[[field + "_observed" for field in profitability_fields]].all(axis=1)
    aggregates = {field + "_observed": (field + "_observed", "sum") for field in FINANCIAL_FIELDS}
    rows = merged.groupby("date").agg(pool=("symbol", "size"),
        profitability_observed=("profitability_observed", "sum"), **aggregates).reset_index()
    for field in FINANCIAL_FIELDS:
        rows[field + "_coverage"] = rows[field + "_observed"] / rows.pool
    rows["roe_observed"] = rows.roeAvg_observed
    rows["roe_coverage"] = rows.roe_observed / rows.pool
    rows["profitability_coverage"] = rows.profitability_observed / rows.pool
    return rows, {"view": str(view), "declared_symbols": receipt["declared_symbols"],
                  "completed_symbol_histories": receipt["symbols"], "mean_monthly_roe_coverage": float(rows.roe_coverage.mean()),
                  "min_monthly_roe_coverage": float(rows.roe_coverage.min()),
                  "mean_monthly_field_coverage": {field: float(rows[field + "_coverage"].mean()) for field in FINANCIAL_FIELDS},
                  "min_monthly_field_coverage": {field: float(rows[field + "_coverage"].min()) for field in FINANCIAL_FIELDS},
                  "profitability_fields": list(profitability_fields),
                  "mean_monthly_profitability_coverage": float(rows.profitability_coverage.mean()),
                  "min_monthly_profitability_coverage": float(rows.profitability_coverage.min()),
                  "affects_strategy_pool": False,
                  "quality_strategy_replayed": False, "revision_safe_pit_verified": False}


def coverage_run(identifier, financial_view):
    source_path = ROOT / "outputs/market_replay" / identifier / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("research_family") != ID or source["status"] != "completed_economic_value_diagnostics":
        raise ValueError("Complete fixed valuation ledger required for financial coverage consumption")
    panel_path = Path(source["rankings"]).parent / "monthly_value_pool.parquet"
    pools = pd.read_parquet(panel_path, columns=["date", "symbol"])
    expected = source["pool_summary"]
    if (len(pools) != expected["common_rows"] or pools.symbol.nunique() != expected["distinct_pool_symbols"]
            or pools.date.nunique() != expected["decision_months"]):
        raise ValueError("Saved fixed valuation pool identity/count changed")
    coverage, summary = financial_coverage(financial_view, pools)
    root = DATA_ROOT / "research/value_financial_coverage" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    coverage.to_csv(root / "monthly_coverage.csv", index=False)
    previous = source.get("financial_coverage")
    result = {"status": "completed_fixed_pool_financial_coverage", "source_run": str(source_path),
              "fixed_pool": str(panel_path), "previous_coverage": previous, "coverage": summary,
              "account_replayed": False, "rankings_changed": False, "quality_strategy_replayed": False,
              "execution_admitted": False}
    write_json(root / "result.json", result)
    print(json.dumps({"folder": str(root), **result}), flush=True)
    return result


def prepare_actions(identifier, primary_view, max_fresh=64, *, profitability=False, qarp=False, stockdb=False):
    """Reuse public histories for all fixed buffers, never select by event coverage."""
    from .action_events import amend_secondary
    from .action_terms import collect

    if not 0 <= max_fresh <= 64:
        raise ValueError("At most64 new public source histories per batch")
    if sum((profitability, qarp, stockdb)) > 1:
        raise ValueError("Select one fixed signal family")
    start, end = START, END
    family, cases, label = ID, CASES, "value"
    families = {family}
    statuses = {"failed_economic_value_diagnostics", "completed_economic_value_diagnostics"}
    if profitability:
        from .profitability_portfolios import ID as family, COHORT_ID, FIXED_PERIOD_ID, CASES as cases
        families = {family, COHORT_ID, FIXED_PERIOD_ID}
        label = "profitability"
        statuses = {"failed_profitability_diagnostics", "completed_profitability_diagnostics"}
    if qarp:
        from .profitability_inputs import QARP_ID as family, QARP_CASES as cases
        families, label = {family}, "qarp"
        statuses = {"failed_qarp_diagnostics", "completed_qarp_diagnostics"}
    if stockdb:
        from .stockdb_factor_replay import CASES as cases
        from .stockdb_factor_diagnostics import START as start, END as end
        label = "stockdb_factor"
        statuses = {"blocked_factor_ledger_diagnostics", "completed_factor_ledger_diagnostics"}
    source_path = ROOT / "outputs" / ("stockdb_factor_replay" if stockdb else "market_replay") / identifier / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if stockdb:
        plan = source["plan"]
        if (source["status"] not in statuses or [source["start"], source["end"]] != [start, end]
                or [plan["start"], plan["end"]] != [start, end]
                or not plan["cases"] or not set(plan["cases"]) <= set(cases)
                or source["execution_admitted"] or plan["execution_admitted"]):
            raise ValueError("Terminal unadmitted fixed2023 factor replay required")
        inputs = plan["stock_inputs"]
    else:
        if (source.get("research_family") not in families or source["status"] not in statuses
                or source["protocol"]["signals"] != list(cases)):
            raise ValueError("Terminal fixed signal ranks required before data preparation")
        inputs = source["protocol"]["stock_inputs"]
    primary = json.loads((Path(primary_view) / "result.json").read_text(encoding="utf-8"))
    effective = json.loads((Path(inputs["event_view"]) / "result.json").read_text(encoding="utf-8"))
    if Path(primary["raw_view"]).resolve() != Path(effective["raw_view"]).resolve():
        raise ValueError("Public history preparation must use the original raw scope")
    if stockdb:
        # The saved rank file covers both fixed arms even when only one was replayed.
        buffers = pd.read_csv(Path(source["artifacts"]) / "rankings.csv")
        if (set(buffers.case) != set(cases) or buffers.duplicated(["date", "case", "rank"]).any()
                or not buffers.date.between(start, end).all()
                or any(sorted(batch["rank"]) != list(range(1, plan["portfolio"]["retain_rank"] + 1))
                       for _, batch in buffers.groupby(["date", "case"]))):
            raise ValueError("Complete saved fixed-factor rank buffers required")
    else:
        buffers = pd.concat([pd.read_csv(Path(source["rankings"]) / case / "rankings.csv") for case in cases])
    actions = pd.read_parquet(Path(inputs["event_view"]) / "canonical_actions.parquet").fillna("")
    pending = actions.loc[actions.symbol.isin(buffers.symbol) & actions.dividOperateDate.between(start, end)
                          & actions.dividReserveToStockPs.eq("")]
    keys, remaining = set(zip(pending.symbol, pending.dividOperateDate)), set(pending.symbol)
    batches = []
    archive_root = DATA_ROOT / "research/secondary_action_terms"
    unavailable_sources = set()
    for path in sorted(archive_root.glob("*/result.json"), reverse=True):
        archived = json.loads(path.read_text(encoding="utf-8"))
        if archived["status"] != "completed_secondary_action_terms":
            continue
        available = {row["symbol"] for row in archived["records"] if row["status"] == "source_histories_compared"}
        unavailable_sources.update(row["symbol"] for row in archived["records"] if row["status"] == "source_error")
        chosen = sorted(available & remaining)
        if chosen:
            batches.append((chosen, path.parent))
            remaining.difference_update(chosen)
    counts = buffers.symbol.value_counts()
    failed = {row["error"].split("corporate action: ", 1)[1].split()[0] for row in source["results"]
              if "Unresolved held corporate action: " in row.get("error", "")}
    deferred = sorted(remaining & unavailable_sources)
    chosen = sorted(remaining - unavailable_sources,
                    key=lambda symbol: (symbol not in failed, -int(counts.get(symbol, 0)), symbol))[:max_fresh]
    if chosen:
        batches.append((chosen, None))
    folder = DATA_ROOT / ("research/" + label + "_action_preparation") / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    buffers.to_csv(folder / "fixed_rank_buffers.csv", index=False)
    pending.to_csv(folder / "pending_events.csv", index=False)
    result = {"status": "running_" + label + "_action_preparation", "source_run": str(source_path), "steps": [],
              "original_inputs": inputs, "pending_events": len(keys), "pending_symbols": len(set(pending.symbol)),
              "fresh_symbols": chosen, "deferred_source_error_symbols": deferred,
              "matched_events": [], "execution_admitted": False,
              "limits": ["Fixed-buffer source acquisition, not an event-quality universe filter",
                         "Literal secondary zero matched to primary cash/dates, not independent issuer/PIT admission",
                         "Original rank/cost/model unchanged; historical prefix changes require explicit replay"]}
    event_view, reference_view = inputs["event_view"], inputs["reference_view"]
    write_json(folder / "result.json", result)
    try:
        for symbols, reuse_root in batches:
            root, receipt = collect(primary_view, symbols, reuse_root=reuse_root, retain_archived=False)
            matched = sorted({(c["symbol"], c["date"]) for row in receipt["records"] for c in row.get("candidates", [])
                              if (c["symbol"], c["date"]) in keys})
            if matched:
                event_view, reference_view, _, _ = amend_secondary(event_view, reference_view, root, matched)
                keys.difference_update(matched)
                result["matched_events"].extend(matched)
            result["steps"].append({"source": str(root), "reused_symbols": receipt["reused_symbols"],
                                    "source_errors": receipt["source_errors"], "new_matches": len(matched)})
            result.update(event_view=str(event_view), reference_view=str(reference_view), pending_events=len(keys))
            write_json(folder / "result.json", result)
        result["status"] = "completed_" + label + "_action_preparation"
    except Exception as error:
        result.update(status="failed_" + label + "_action_preparation", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result.update(event_view=str(event_view), reference_view=str(reference_view), pending_events=len(keys))
        write_json(folder / "result.json", result)
        print(json.dumps({"folder": str(folder), **result}), flush=True)
    return result


def report(out, record):
    lines = ["# Economic Value: Fixed Monthly Ledger", "", "Development diagnosis only. NO_TRADE.",
             f"{START}..{END};140000CNY; monthly20 names/rank30; unchanged original costs and sole ledger.",
             "", "| Case | Status | Sessions | Net | MDD | Sharpe | Fees CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed | {row['completed_sessions']} | {row['net_return']:.2%} | "
                         f"{abs(row['max_drawdown']):.2%} | {row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} |")
        else:
            lines.append(f"| {row['signal']} | failed | {row['completed_sessions']} | unknown | unknown | unknown | unknown |")
            lines.append(row.get("error", "Unknown input"))
    if record.get("comparisons"):
        lines += ["", "## Same-Pool Net Increment", "",
                  "| Candidate | Matched control | Net difference pp | MDD magnitude difference pp |",
                  "| --- | --- | ---: | ---: |"]
        for row in record["comparisons"]:
            lines.append(f"| {row['candidate']} | {row['control']} | {100 * row['net_difference']:.2f} | "
                         f"{100 * row['drawdown_magnitude_difference']:.2f} |")
    if record.get("financial_coverage"):
        cover = record["financial_coverage"]
        lines += ["", f"Financial source histories: {cover['completed_symbol_histories']}/{cover['declared_symbols']}; "
                  f"mean monthly ROE coverage in this pool: {cover['mean_monthly_roe_coverage']:.2%}. "
                  "Not a full-market quality study and not a filter/zero-filled score."]
    lines += ["", "## Fixed Rules", ""] + [f"- {key}: {value}" for key, value in FORMULAS.items()]
    lines += ["", "## Sources and Adaptation", ""]
    lines += [f"- [{item['read']}]({item['url']}): {item['lesson']}" for item in SOURCES]
    lines += ["", "## Boundaries", ""] + ["- " + item for item in LIMITS]
    if record.get("error"):
        lines += ["", record["error"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id=None, *, financial_view=None, resume_id=None, event_view=None, reference_view=None, refresh_prefixes=False):
    previous = None
    if resume_id:
        previous = json.loads((ROOT / "outputs/market_replay" / resume_id / "run.json").read_text(encoding="utf-8"))
        if (previous.get("research_family") != ID or previous["status"] not in {
                "completed_economic_value_diagnostics", "failed_economic_value_diagnostics"}
                or previous["protocol"]["formulas"] != FORMULAS or previous["protocol"]["signals"] != list(CASES)):
            raise ValueError("Same terminal fixed valuation experiment required for continuation")
        source_path = Path(previous["source_run"])
        financial_view = previous["protocol"]["financial_view"]
        bound = previous["protocol"]["stock_inputs"]
    else:
        source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_family_portfolio_diagnostics" or source.get("research_family") != "fixed_family_incremental"
            or source["start"] != START or source["end"] != END):
        raise ValueError("Completed original full-source monthly family experiment required")
    inputs = bind_stock_inputs(bound if previous else source["protocol"]["stock_inputs"], event_view, reference_view)
    config = portfolio()
    if source["protocol"]["signal_portfolio_configs"]["stock_low_vol_control"] != config.to_dict():
        raise ValueError("Original monthly selector/cost config changed")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": ID, "signals": list(CASES), "formulas": FORMULAS, "portfolio": config.to_dict(),
                "signal_portfolio_configs": {case: config.to_dict() for case in CASES}, "stock_inputs": inputs,
                "financial_view": str(Path(financial_view).resolve()) if financial_view else None,
                "economic_orientation": "High signed E/P and positive B/P; economic priors, no fitted direction",
                "selection": "Monthly20/rank30,4% equity targets,80% requested gross, other rules identical to original low-risk controls",
                "common_pool": "Original source finite252-vol/12-1momentum/liquidity/nativeRSQR30/WVMA20 pool intersected with dated finite nonzeroPE and positivePB",
                "sources": SOURCES, "limitations": LIMITS, "parameters_searched": False, "models_fitted": False,
                "fresh_holdout_claimed": False, "execution_admitted": False}
    record = {"id": identifier, "status": "running", "research_family": ID, "source_run": str(source_path.resolve()),
              "start": START, "end": END, "sessions": source["sessions"], "artifacts": str(artifacts),
              "protocol": protocol, "results": [], "comparisons": [], "execution_admitted": False,
              "purpose": ID, "scope": "full_source_sh_sz_matched_valuation_pool_research_only",
              "dataset": inputs["price_view"],
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
            original = previous["protocol"]
            if any(original.get(key) != protocol.get(key) for key in ("portfolio", "signal_portfolio_configs", "financial_view", "selection", "common_pool")):
                raise ValueError("Saved value account rules/costs/coverage changed")
            rankings = Path(previous["rankings"])
            record.update(rankings=str(rankings), pool_summary=previous["pool_summary"],
                          financial_coverage=previous.get("financial_coverage"))
        else:
            anchors = [day for day in month_ends(stocks.calendar, END) if day >= START]
            helper = LowRiskFeed(stocks, features(stocks, END), VARIANTS[1], END, diagnostic_extension=True)
            native = read_native_monthends(stocks, inputs["native_view"], anchors)
            pools, audits = [], []
            rankings = artifacts / "predictions"
            ranking_rows = {case: [] for case in CASES}
            for day in anchors:
                pool = helper.pool(day, config)
                if not isinstance(pool, list):
                    raise ValueError(f"Original price pool unavailable: {day}: {pool}")
                shared = common_pool(pool, native.loc[pd.Timestamp(day)])
                frame, audit = value_pool(shared, stocks.rows.get(day))
                audits.append({"date": day, "original_price_pool": len(pool), **audit})
                pools.append(frame.reset_index().assign(date=day))
                for case in CASES:
                    ranked = ranked_value(frame, case)
                    if len(ranked) < config.retain_rank:
                        raise ValueError(f"Insufficient fixed30 valuation rank buffer: {day}: {case}")
                    ranking_rows[case].extend({"date": day, "rank": i, **row}
                                             for i, row in enumerate(ranked[:config.retain_rank], 1))
            panel = pd.concat(pools, ignore_index=True)
            panel.to_parquet(artifacts / "monthly_value_pool.parquet", index=False)
            pd.DataFrame(audits).to_csv(artifacts / "pool_counts.csv", index=False)
            record.update(rankings=str(rankings), pool_summary={"decision_months": len(anchors), "common_rows": len(panel),
                "distinct_pool_symbols": int(panel.symbol.nunique()), "min_common_pool": min(row["value_common_pool"] for row in audits),
                "max_common_pool": max(row["value_common_pool"] for row in audits)})
            for case in CASES:
                folder = rankings / case
                folder.mkdir(parents=True)
                pd.DataFrame(ranking_rows[case]).to_csv(folder / "rankings.csv", index=False)
            if financial_view:
                coverage, summary = financial_coverage(financial_view, panel)
                coverage.to_csv(artifacts / "financial_coverage.csv", index=False)
                record["financial_coverage"] = summary
            print(f"Fixed valuation inputs: {record['pool_summary']}; no fit or parameter search", flush=True)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        for case in CASES:
            checkpoint = None
            if previous:
                prior = next(row for row in previous["results"] if row["signal"] == case)
                checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / case, config, dates)
                if len(checkpoint["daily"]) != prior["completed_sessions"]:
                    raise ValueError("Saved valuation case is not its terminal prefix")
                try:
                    ignored = validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                                     checkpoint["state"]["day"], checkpoint["state"])
                except ValueError as error:
                    if not refresh_prefixes:
                        raise
                    record.setdefault("explicit_historical_prefix_repairs", {})[case] = str(error)
                    checkpoint, ignored = None, []
                record.setdefault("uninvolved_source_changes", {})[case] = ignored or []
            feed = SavedRankFeed(stocks, rankings / case, ID + ":" + case, config, dates)
            folder = artifacts / case
            result = {"signal": case, **baseline_ledger_result(feed, config, dates, folder, checkpoint=checkpoint)}
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(folder, config.initial_cash)
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/{len(dates)} net={result.get('net_return')} {result.get('error', '')}", flush=True)
        by_case = {row["signal"]: row for row in record["results"]}
        for candidate in CASES[2:]:
            for control in CONTROLS:
                left, right = by_case[candidate], by_case[control]
                if left["status"] == right["status"] == "completed":
                    record["comparisons"].append({"candidate": candidate, "control": control,
                        "net_difference": left["net_return"] - right["net_return"],
                        "drawdown_magnitude_difference": abs(left["max_drawdown"]) - abs(right["max_drawdown"]),
                        "fee_difference": left["fees"] - right["fees"]})
        record["status"] = ("completed_economic_value_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "failed_economic_value_diagnostics")
    except Exception as error:
        record.update(status="failed_economic_value_diagnostics", error=f"{type(error).__name__}: {error}")
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
    group.add_argument("--resume-run")
    group.add_argument("--prepare-actions")
    group.add_argument("--coverage-run")
    parser.add_argument("--financial-view", type=Path)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--primary-view", type=Path)
    parser.add_argument("--max-fresh", type=int, default=64)
    parser.add_argument("--refresh-prefixes", action="store_true")
    args = parser.parse_args(argv)
    if args.coverage_run:
        if not args.financial_view or args.event_view or args.reference_view or args.primary_view or args.refresh_prefixes:
            parser.error("Coverage consumption needs one new financial-view, with saved pool/ranks/accounts unchanged")
        coverage_run(args.coverage_run, args.financial_view)
        return 0
    if args.prepare_actions:
        if not args.primary_view or args.financial_view or args.event_view or args.reference_view or args.refresh_prefixes:
            parser.error("Action preparation requires primary-view and a terminal run, not changed strategy inputs")
        prepare_actions(args.prepare_actions, args.primary_view, args.max_fresh)
        return 0
    if args.primary_view or args.refresh_prefixes and not args.resume_run:
        parser.error("Primary view is for action preparation; prefix repair requires a terminal continuation")
    if args.resume_run and args.financial_view:
        parser.error("A resumed financial source cannot change")
    result = run(args.source_run, financial_view=args.financial_view, resume_id=args.resume_run,
                 event_view=args.event_view, reference_view=args.reference_view, refresh_prefixes=args.refresh_prefixes)
    return 0 if result["status"] == "completed_economic_value_diagnostics" else 1


if __name__ == "__main__":
    raise SystemExit(main())
