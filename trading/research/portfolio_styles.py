"""Compare saved candidate, target and held styles in the same frozen financial pool."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import normalize_symbol
from .. import replay_reports
from ..rules import cents, execution_rule, notional_cents, quote_value
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .factors import LABEL
from .holding_paths import held_sessions
from .profitability_inputs import QARP_ID
from .profitability_portfolios import FIXED_PERIOD_ID
from .style_exposure import FIELDS, STYLE, exposure_row, load_industries, style_day
from .turnover_attribution import action_map, project_orders


LIMITS = [
    "Frozen available-financial subset, inspected development data; not full market, independent validation or verified PIT",
    "Price*volume/turn is only a size proxy with unverified scale, not actual RMB market capitalization",
    "Style ranks use the identical frozen pool on each decision date; held securities outside that pool stay unknown",
    "Industry uses exact archived requested dates only; no queries, current-classification backfill or missing-date propagation",
    "Provider historical industry/PE/PB fields are not independently verified revision-safe vintages",
    "Head candidates, positive target budgets and actually held shares are distinct baskets",
    "Held weights use saved quantities and same-close raw marks with Ledger rounding, before the next-session target execution",
    "Style means condition on known basket weight, with coverage retained; incomplete full-period industry conclusions are unknown",
    "Monthly snapshots are descriptive exposures, not daily factor-return attribution, causal alpha or a neutral traded account",
    "No future labels, accounts replayed, price repair, parameter search, defaults changed or execution admission; NO_TRADE",
]
STYLE_COLUMNS = [*STYLE, "size_bucket", LABEL, "label_minus_size_peer", "size_peer_observed_fraction"]


def join_styles(basket, styles, industries):
    result = basket.merge(styles[["date", "symbol", *STYLE_COLUMNS]],
                          on=["date", "symbol"], how="left", validate="one_to_one", indicator="pool_match")
    result["size_bucket"] = result.size_bucket.fillna("unknown")
    result = result.merge(industries, on=["date", "symbol"], how="left", validate="one_to_one")
    result["industry"] = result.industry.fillna("")
    return result


def basket_observation(basket, day, unknown_marks=0):
    if basket.empty or unknown_marks:
        result = {"date": day, "targets": len(basket), "gross_target": None if unknown_marks else 0,
                  "unknown_marks": unknown_marks, "pool_weight_coverage": None}
        for field in STYLE:
            result.update({field + "_mean": None, field + "_coverage": None})
        result.update({"size_" + bucket + "_fraction": None for bucket in ["Q1", "Q2", "Q3", "Q4", "Q5", "unknown"]})
        return result | {"industry_coverage": None, "largest_known_industry_fraction": None}
    result = exposure_row(basket, day)
    for field in [LABEL, "label_minus_size_peer", "size_peer_observed_fraction"]:
        for suffix in ["_mean", "_coverage"]:
            result.pop(field + suffix)
    result.update(unknown_marks=0, pool_weight_coverage=float(
        basket.loc[basket.pool_match.eq("both"), "weight"].sum() / basket.weight.sum()))
    return result


def holding_basket(shares, raw, mark):
    result = shares.merge(raw[["date", "symbol", "close"]],
                          on=["date", "symbol"], how="left", validate="one_to_one")
    equity = cents(mark["equity"])
    if equity <= 0 or not result.date.eq(mark["date"]).all():
        raise ValueError("Positive equity and same-date saved holdings/marks required")
    values = []
    for row in result.to_dict("records"):
        quantity = row["quantity"]
        if not np.isfinite(quantity) or quantity <= 0 or quantity != int(quantity):
            raise ValueError("Positive integral recorded holding quantity required")
        price = row["close"]
        if not np.isfinite(price) or price <= 0:
            values.append(None)
        else:
            unit = execution_rule(row["symbol"], row["date"]).price_unit
            values.append(notional_cents(quote_value(price, unit), int(quantity), unit))
    unknown = sum(value is None for value in values)
    # Receivables are part of equity but are not tradable stock exposure.
    invested = equity - cents(mark["cash"]) - cents(mark["receivables"])
    if invested < 0 or (not unknown and sum(values) != invested):
        raise ValueError("Same-close holding values disagree with the original saved Ledger mark")
    result["marked_value_cny"] = pd.Series(values, index=result.index, dtype=float) / 100
    result["weight"] = result.marked_value_cny * 100 / equity
    return result, unknown


def summarize_dates(frame):
    active = frame.loc[frame.targets.gt(0) & frame.unknown_marks.eq(0)]
    complete = active.loc[np.isclose(pd.to_numeric(active.industry_coverage), 1, rtol=0, atol=1e-12)]
    return {"anchors": len(frame), "nonempty_known_anchors": len(active),
            "unknown_valuation_anchors": int(frame.unknown_marks.gt(0).sum()),
            "anchor_means_condition_on_known_weight": active.mean(numeric_only=True).to_dict(),
            "complete_industry_anchors": len(complete),
            "full_anchor_mean_largest_industry_fraction": float(complete.largest_known_industry_fraction.mean())
            if len(active) and len(complete) == len(active) else None,
            "observed_complete_industry_dates": list(complete.date)}


def industry_groups(basket):
    values = basket.copy()
    values["industry"] = values.industry.replace("", "unknown")
    groups = values.groupby(["date", "industry"], as_index=False).agg(
        securities=("symbol", "size"), equity_weight=("weight", "sum"))
    totals = values.groupby("date").weight.sum()
    groups["stock_basket_fraction"] = groups.equity_weight / groups.date.map(totals)
    return groups


def report(out, record):
    def fmt(value):
        return "unknown" if value is None or not np.isfinite(value) else f"{value:.2%}"
    lines = ["# Saved Financial-Pool Portfolio Styles", "", "Status: " + record["status"] + ". NO_TRADE.", "",
             f"Same frozen pool: {record['pool_rows']} rows / {record['pool_symbols']} stocks / {len(record['anchors'])} anchors.",
             f"Exact archived industry matches: {len(record['industry_dates'])}/{len(record['anchors'])} anchors.",
             "Missing industries remain unknown. No account replay or future return labels.", "",
             "Head = first20 equal-weight candidates; target = recorded positive budgets; held = saved shares valued at that month's close.",
             "Held snapshot precedes execution of the newly generated target; gross held excludes unpaid receivables.", "",
             "|Case|Basket|Nonempty anchors|Gross stock weight|Proxy-size rank / coverage|Q1 smallest proxy share|Positive PB rank / coverage|Complete industry anchors|",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for case in record["cases"]:
        for basket, summary in case["baskets"].items():
            mean = summary["anchor_means_condition_on_known_weight"]
            lines.append(f"|{case['signal']}|{basket}|{summary['nonempty_known_anchors']}|{fmt(mean.get('gross_target'))}|"
                         f"{fmt(mean.get('size_proxy_percentile_mean'))} / {fmt(mean.get('size_proxy_percentile_coverage'))}|"
                         f"{fmt(mean.get('size_Q1_fraction'))}|{fmt(mean.get('positive_pb_percentile_mean'))} / "
                         f"{fmt(mean.get('positive_pb_percentile_coverage'))}|{summary['complete_industry_anchors']}|")
    lines += ["", "Means average nonempty monthly anchors and condition on known basket weight, NOT all-day exposure.",
              "Unknown valuation anchors are not silently partially marked; coverage and all-date member/anchor CSVs are saved.",
              "The original net-return metrics are copied unchanged in run.json; exposures do not establish why returns differ.",
              "", "## Observed Held Industry Snapshots", "",
              "Only exact archived dates below, NOT a full-period maximum or causal return attribution.",
              "|Case|Date|Largest observed sector|Share of stocks|Share of account equity|",
              "|---|---|---|---:|---:|"]
    for case in record["cases"]:
        for row in case["held_industry_snapshots"]:
            lines.append(f"|{case['signal']}|{row['date']}|{row['industry']}|{fmt(row['stock_basket_fraction'])}|"
                         f"{fmt(row['equity_weight'])}|")
    lines += ["", "Unknown sectors are explicit in industry_groups.csv. No classification is propagated to missing dates.",
              "", "## Boundaries", *["- " + value for value in LIMITS], "", "Artifacts: " + record["artifacts"]]
    if record.get("error"):
        lines.append("Failure: " + record["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(run_ids, input_bridge, industry_view=None):
    bridge_path = Path(input_bridge).resolve()
    bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
    if bridge["status"] != "completed_frozen_profitability_valuation" or bridge.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted frozen financial/valuation bridge required")
    pool = pd.read_parquet(Path(bridge["artifacts"]) / "monthly_feature_inputs.parquet")
    if len(pool) != bridge["pool_rows"] or pool.duplicated(["date", "symbol"]).any():
        raise ValueError("Frozen financial pool identities changed")
    days = sorted(set(pool.date))
    source_path = Path(bridge["source_run"]).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    raw_view = source["protocol"]["stock_manifest"]["raw_view"]
    raw = pq.read_table(Path(raw_view) / "raw_daily.parquet", columns=["date", "code", "adjustflag", *FIELDS],
                        filters=[("date", "in", days)]).to_pandas()
    raw["symbol"] = raw.code.map(lambda value: normalize_symbol(value).vt_symbol)
    if raw.duplicated(["date", "symbol"]).any() or not raw.adjustflag.eq("3").all():
        raise ValueError("Distinct unadjusted raw quote rows required")
    for field in FIELDS:
        raw[field] = pd.to_numeric(raw[field], errors="coerce").replace([np.inf, -np.inf], np.nan)
    matched = pool[["date", "symbol", "peTTM", "pbMRQ"]].merge(raw.drop(columns=["code", "adjustflag"]),
        on=["date", "symbol"], how="left", validate="one_to_one", suffixes=("_pool", ""), indicator=True)
    if not matched._merge.eq("both").all() or any(not np.allclose(matched[field], matched[field + "_pool"],
            equal_nan=True, rtol=1e-12, atol=0) for field in ("peTTM", "pbMRQ")):
        raise ValueError("Financial bridge no longer matches same-date raw valuation fields")
    matched[LABEL] = np.nan
    styles = pd.concat([style_day(frame)[0] for _, frame in matched.groupby("date", sort=True)], ignore_index=True)
    industries, industry_receipt = load_industries(industry_view, days)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/portfolio_styles" / identifier, DATA_ROOT / "research/portfolio_styles" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    styles[["date", "symbol", *FIELDS, *STYLE, "size_bucket"]].to_parquet(artifacts / "dated_style_pool.parquet", index=False)
    record = {"id": identifier, "status": "running", "runs": run_ids, "input_bridge": str(bridge_path),
              "artifacts": str(artifacts), "raw_view": raw_view, "pool_rows": len(pool), "pool_symbols": pool.symbol.nunique(),
              "anchors": days, "industry_dates": sorted(set(industries.date)), "industry_view": str(industry_view) if industry_view else None,
              "industry_receipt_status": industry_receipt["status"], "cases": [], "accounts_replayed": False,
              "execution_admitted": False, "limits": LIMITS}
    write_json(out / "run.json", record)
    try:
        for run_id in run_ids:
            saved, account_root = replay_reports._read(run_id)
            family = saved.get("research_family")
            original = family == FIXED_PERIOD_ID and (ROOT / "outputs/market_replay" / run_id / "run.json").resolve() == source_path
            derived = family == QARP_ID and Path(saved["protocol"]["inputs_run"]).resolve() == bridge_path
            if not (original or derived) or Path(saved["protocol"]["stock_manifest"]["raw_view"]).resolve() != Path(raw_view).resolve():
                raise ValueError("Only original matched financial accounts and their frozen QARP consumers are supported")
            actions = action_map(saved, run_id)
            for case in saved["results"]:
                signal = case["signal"]
                detail = replay_reports.detail(run_id, signal, include_marks=True)
                if detail["status"] != "completed":
                    raise ValueError("Completed original account required")
                folder = account_root / saved.get("ledger_folders", {}).get(signal, signal)
                account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
                observations = {}
                project_orders(account, actions, daily_holdings=observations)
                shares = held_sessions(detail["daily_marks"], observations)
                marks = {mark["date"]: mark for mark in detail["daily_marks"]}
                targets = [event for event in account["events"] if event["kind"] == "targets_generated"]
                if [event["signal_date"] for event in targets] != days:
                    raise ValueError("Saved target calendar differs from the frozen comparison pool")
                ranks = pd.read_csv(Path(saved["rankings"]) / signal / "rankings.csv", dtype={"date": str})
                members, dates = {key: [] for key in ("head", "target", "held")}, {key: [] for key in ("head", "target", "held")}
                for target in targets:
                    day = target["signal_date"]
                    if target["day"] != day or target["execution_not_before"] <= day:
                        raise ValueError("Prior-close decision and subsequent execution required")
                    n = detail["config"]["holdings"]
                    head = ranks.loc[ranks.date.eq(day) & ranks["rank"].le(n), ["date", "symbol"]].assign(weight=1 / n)
                    if len(head) != n:
                        raise ValueError("Saved head candidates are incomplete")
                    budget = pd.DataFrame([{"date": day, "symbol": symbol, "weight": weight}
                        for symbol, weight in target["targets"].items() if weight > 0], columns=["date", "symbol", "weight"])
                    held, unknown = holding_basket(shares.loc[shares.date.eq(day)], raw.loc[raw.date.eq(day)], marks[day])
                    for name, basket in (("head", head), ("target", budget), ("held", held)):
                        joined = join_styles(basket, styles.loc[styles.date.eq(day)], industries.loc[industries.date.eq(day)])
                        members[name].append(joined.drop(columns=[LABEL, "label_minus_size_peer", "size_peer_observed_fraction"]))
                        dates[name].append(basket_observation(joined, day, unknown if name == "held" else 0))
                destination = artifacts / (run_id + "_" + signal)
                destination.mkdir()
                summaries, industry_pieces = {}, []
                for name in members:
                    combined = pd.concat(members[name], ignore_index=True)
                    combined.to_csv(destination / (name + "_members.csv"), index=False)
                    observed = combined.loc[combined.date.isin(record["industry_dates"])]
                    if len(observed) and np.isfinite(observed.weight).all():
                        industry_pieces.append(industry_groups(observed).assign(basket=name))
                    dated = pd.DataFrame(dates[name])
                    dated.to_csv(destination / (name + "_dates.csv"), index=False)
                    summaries[name] = summarize_dates(dated)
                grouped = pd.concat(industry_pieces, ignore_index=True) if industry_pieces else pd.DataFrame(
                    columns=["date", "industry", "securities", "equity_weight", "stock_basket_fraction", "basket"])
                grouped.to_csv(destination / "industry_groups.csv", index=False)
                largest = grouped.loc[grouped.basket.eq("held") & grouped.industry.ne("unknown")].sort_values(
                    ["date", "equity_weight"], ascending=[True, False]).drop_duplicates("date")
                record["cases"].append({"run": run_id, "signal": signal, "source_account": str(folder / "account.json"),
                    "original_metrics": detail["metrics"], "baskets": summaries, "held_industry_snapshots": largest.to_dict("records")})
                write_json(out / "run.json", record)
                print(f"{signal}: {len(days)} saved anchors, held={summaries['held']['nonempty_known_anchors']}, "
                      f"complete-industry={summaries['held']['complete_industry_anchors']}", flush=True)
        record["status"] = ("completed_saved_portfolio_styles" if len(record["industry_dates"]) == len(days)
                            else "completed_saved_portfolio_styles_with_industry_gap")
    except Exception as error:
        record.update(status="failed_saved_portfolio_styles", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--input-bridge", type=Path, required=True)
    parser.add_argument("--industry-view", type=Path)
    args = parser.parse_args(argv)
    if len(set(args.run)) != len(args.run):
        parser.error("Saved run identifiers must be distinct")
    run(args.run, args.input_bridge, args.industry_view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
