"""Evaluate the 200 existing factor-mining templates across the broad research universe."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .factor_risk_audit import daily_metrics, no_exit_shock
from .factors import candidates, catalog
from .full_market import verify_chunks
from .full_market_alpha158 import (FORWARD, FIRST, SELECTION_END, decision_dates, evaluate_both,
                                   fetch_features, qcode, rank_summary, select_candidates)
from .full_market_mix import (BUY_COST, END, GROSS, HOLDINGS, MARKET_GROUPS, SELL_COST, SPLIT,
                              load_rows, market_panel, proxy_replay)


WINDOWS = (5, 10, 20, 30, 60)
BASE_FAMILIES = ("ROC", "VMA", "RSV", "BETA", "CORR")


def source_features(provider, symbols, decision_grid):
    definitions = catalog().set_index("name")
    names = [f"{family}{window}" for family in BASE_FAMILIES for window in WINDOWS]
    formulas = definitions.loc[names, "formula"].tolist()
    names.extend(f"RETVOL{window}" for window in WINDOWS)
    formulas.extend(f"Std($close/Ref($close,1)-1,{window})" for window in WINDOWS)
    fetched = fetch_features(provider, symbols, formulas, names)
    fetched = fetched.loc[fetched.date.isin(decision_grid)].copy()
    fetched["date"] = pd.to_datetime(fetched.date.astype(str), format="%Y%m%d")
    fetched = fetched.set_index(["date", "code"])[names].sort_index()
    return candidates(fetched, WINDOWS)


def run(alpha_run, out=None):
    import qlib

    alpha_run = Path(alpha_run)
    alpha_record = json.loads((alpha_run / "protocol.json").read_text(encoding="utf-8"))
    if alpha_record["status"] != "completed_research_only" or alpha_record["completed_definitions"] != 158:
        raise ValueError("A completed full-market Alpha158 provider is required")
    snapshot = Path(alpha_record["snapshot"])
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != alpha_record["snapshot_manifest_sha256"]:
        raise ValueError("Snapshot changed since the Alpha158 run")
    manifest = json.loads(manifest_bytes)
    verify_chunks(snapshot, manifest)
    group = alpha_record["group"]
    groups = {name: (prefixes, amount, name == "bse_equities")
              for name, prefixes, amount in MARKET_GROUPS}
    prefixes, min_amount, bse = groups[group]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8] + "_" + group
    out = Path(out) if out else ROOT / "outputs" / "full_market_templates" / identifier
    out.mkdir(parents=True, exist_ok=False)
    record = {"status": "running", "alpha158_run": str(alpha_run), "snapshot": str(snapshot),
              "snapshot_manifest_sha256": alpha_record["snapshot_manifest_sha256"],
              "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "group": group,
              "expected_templates": 200, "completed_templates": 0, "account_backtest": False,
              "trading_authorized": False, "fresh_holdout_claimed": False}
    write_json(out / "protocol.json", record)
    try:
        frame = load_rows(snapshot, manifest, prefixes)
        _, eligible, pct, tradable, buyable, sellable, coverage = market_panel(frame, min_amount, bse=bse)
        source = frame.loc[frame.date.between(FIRST, END) & frame.volume.gt(0) & frame.is_st.eq(False)]
        stress_days = source.groupby("date").pct_chg.median().nsmallest(10).index.tolist()
        del frame
        calendar = pct.index.tolist()
        sessions = [day for day in calendar if FIRST <= day <= END]
        decision_grid = sessions[::FORWARD]
        decisions = decision_dates(calendar)
        index = (1 + pct.fillna(0)).cumprod().where(pct.notna())
        label = index.shift(-FORWARD) / index - 1
        provider = Path(alpha_record["artifacts"]) / "qlib_provider"
        qlib.init(provider_uri=str(provider), region="cn", expression_cache=None, dataset_cache=None,
                  joblib_backend="threading", kernels=2)
        scores, definitions = source_features(provider, [qcode(code) for code in pct.columns], decision_grid)
        if len(definitions) != 200 or len(scores.columns) != 200:
            raise ValueError("Expected exactly 200 local templates")
        definitions.to_csv(out / "template_catalog.csv", index=False)
        record.update(completed_templates=200, panel_coverage=coverage)
        write_json(out / "protocol.json", record)
        rows = []
        for day, segment in decisions:
            day_scores = scores.loc[pd.Timestamp(str(day))]
            for name in definitions.name:
                for direction, pairs, missing, ic, spread in evaluate_both(
                        day_scores[name], label.loc[day], eligible.loc[day]):
                    rows.append({"group": group, "date": day, "segment": segment, "factor": name,
                                 "direction": direction, "eligible": int(eligible.loc[day].sum()),
                                 "pairs": pairs, "missing_forward_labels": missing,
                                 "rank_ic": ic, "top_minus_universe": spread})
        daily = pd.DataFrame(rows)
        daily.to_csv(out / "daily_template_diagnostics.csv", index=False)
        summary = rank_summary(daily)
        summary.to_csv(out / "template_summary.csv", index=False)
        chosen = select_candidates(summary)
        record["selection_candidates"] = chosen
        selected = {}
        for day in decision_grid:
            day_scores = scores.loc[pd.Timestamp(str(day))]
            allowed = eligible.loc[day]
            selected[day] = {}
            for item in chosen:
                key = item["factor"] + "_" + item["direction"]
                values = day_scores[item["factor"]]
                if item["direction"] == "low":
                    values = -values
                sample = values.loc[allowed.reindex(values.index, fill_value=False)].dropna()
                if len(sample) < 100:
                    raise ValueError(f"Insufficient template coverage on {day}: {key}")
                selected[day][key] = sample.sort_values(ascending=False, kind="stable").head(HOLDINGS).index.tolist()
        periods = {"selection": [day for day in sessions if day <= SELECTION_END],
                   "later_check": [day for day in sessions if day >= SPLIT], "full_period": sessions}
        replays = []
        for item in chosen:
            key = item["factor"] + "_" + item["direction"]
            for period, dates in periods.items():
                period_dates = set(dates)
                picks = {day: value for day, value in selected.items() if day in period_dates}
                result, marks = proxy_replay(dates, picks, pct, tradable, key, BUY_COST, SELL_COST,
                                             buyable, sellable)
                marks.to_csv(out / f"{key}_{period}_daily.csv", index=False)
                metrics = daily_metrics(marks)
                stress = marks.set_index("date").equity.pct_change().fillna(0).reindex(stress_days).dropna() \
                    if period == "full_period" else pd.Series(dtype=float)
                replays.append({"variant": key, "period": period, **metrics,
                                "cost_haircut_cny": result["cost_haircut_cny"],
                                "worst_observed_market_day_return": float(stress.min()) if not stress.empty else None})
        pd.DataFrame(replays).to_csv(out / "candidate_proxy_metrics.csv", index=False)
        record.update(status="completed_research_only", completed_templates=200,
                      decision_dates=len(decisions), candidate_proxy_replays=len(replays),
                      no_exit_80pct_gross_two_day_20pct_loss=no_exit_shock(GROSS, -.20, 2))
        lines = ["# Full-market 200-template research batch", "",
                 f"Group `{group}`; observed codes {coverage['codes']:,}; source rows {coverage['source_rows']:,}.",
                 f"Templates evaluated: **200/200** in both directions; decision dates {len(decisions)}.",
                 "", "Selection used only January-June 2026 diagnostics. All later and full-period",
                 "returns are non-executable StockDB provider-return proxies, not engine/ledger accounts.",
                 "The 2026 data was previously inspected and is not a fresh holdout.",
                 f"An 80%-gross basket has {no_exit_shock(GROSS, -.20, 2):.1%} incremental loss under two",
                 "consecutive -20% no-exit days; none is promoted to paper/live orders.", "",
                 "## Selected candidates", ""]
        if chosen:
            for item in chosen:
                key = item["factor"] + "_" + item["direction"]
                full = next(row for row in replays if row["variant"] == key and row["period"] == "full_period")
                later = next(row for row in replays if row["variant"] == key and row["period"] == "later_check")
                lines.append(f"- {key}: full Sharpe {full['sharpe_zero_rf_252']:.2f}; later "
                             f"{later['sharpe_zero_rf_252']:.2f}; full return {full['return']:.2%}; "
                             f"max drawdown {full['max_drawdown']:.2%}; worst day {full['worst_day']:.2%}.")
        else:
            lines.append("No positive first-segment IC/spread candidate passed the exploratory screen.")
        lines += ["", "See the catalog, per-day diagnostics, summary, proxy replay CSV and protocol for exact coverage.",
                  "ETF history, WorldQuant's complete 101 alphas, and execution-price admission remain outstanding."]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(out / "protocol.json", record)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpha-run", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    print(run(args.alpha_run, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
