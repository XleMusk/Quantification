"""Bounded public-factor mixes on an admitted, fixed A-share snapshot."""

import argparse
from datetime import datetime
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import sys
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..feed import HistoricalFeed
from ..market_snapshot import FIXED_SCOPE
from ..settings import DATA_ROOT, ROOT
from .experiment import ScoreFeed, export_scores, replay, write_json
from .factors import catalog, native_features, research_panel


START = "2026-01-05"
SELECTION_END = "2026-06-30"
CHECK_START = "2026-07-01"
END = "2026-09-24"

# All weights and thresholds are fixed before looking at this experiment's returns.
WEIGHTS = {
    "qlib_only": {"qlib": 1.0},
    "worldquant_101_only": {"worldquant": 1.0},
    "aqr_momentum_only": {"aqr": 1.0},
    "low_vol_only": {"low_vol": 1.0},
    "mix_equal": {"qlib": .25, "worldquant": .25, "aqr": .25, "low_vol": .25},
    "mix_trend": {"qlib": .25, "worldquant": .15, "aqr": .40, "low_vol": .20},
    "mix_defensive": {"qlib": .20, "worldquant": .10, "aqr": .30, "low_vol": .40},
}


def formulas():
    chosen = catalog().set_index("name").loc[["ROC60", "RSV20"]].reset_index()
    extra = pd.DataFrame([{"name": "MOM252_SKIP21", "formula": "Ref($close,21)/Ref($close,252)-1",
                           "family": "AQR_style_momentum", "category": "public_style_proxy",
                           "note": "252-to-21-session price momentum; not AQR production weights"}])
    return pd.concat([chosen, extra], ignore_index=True)


def score_matrix(native, raw, eligible):
    bars = raw.copy()
    bars["date"] = pd.to_datetime(bars.date)
    bars = bars.set_index(["date", "symbol"]).reindex(native.index)
    # WorldQuant paper Alpha#101 is evaluated at the close and traded no earlier
    # than the next session. Raw OHLC preserves its literal +0.001 denominator.
    source = pd.DataFrame({
        "qlib_roc60": 1 / native.ROC60.where(native.ROC60.ne(0)) - 1,
        "qlib_rsv20": native.RSV20,
        "worldquant": (bars.close - bars.open) / (bars.high - bars.low + .001),
        "aqr": native.MOM252_SKIP21,
        "low_vol": -native.RETVOL60,
    }, index=native.index).replace([np.inf, -np.inf], np.nan)
    allowed = eligible.reindex(source.index, fill_value=False).astype(bool)
    allowed &= source.index.get_level_values("date") >= pd.Timestamp(START)
    allowed &= source.index.get_level_values("date") <= pd.Timestamp(END)
    complete = source.loc[allowed].dropna()
    ranks = complete.groupby(level="date").rank(pct=True)
    components = pd.DataFrame({
        "qlib": (ranks.qlib_roc60 + ranks.qlib_rsv20) / 2,
        "worldquant": ranks.worldquant,
        "aqr": ranks.aqr,
        "low_vol": ranks.low_vol,
    }, index=ranks.index)
    scores = pd.DataFrame({name: sum(weight * components[part] for part, weight in weights.items())
                           for name, weights in WEIGHTS.items()}, index=components.index)
    counts = scores.groupby(level="date").size()
    if counts.empty or counts.min() < 50:
        raise ValueError("Fewer than 50 fully observed, eligible stocks on a scored session")
    return scores, {"scored_dates": len(counts), "minimum_eligible_symbols": int(counts.min()),
                    "maximum_eligible_symbols": int(counts.max()), "scored_rows": len(scores)}


def select_on_first_period(rows):
    pool = [row for row in rows if row["variant"] in WEIGHTS and row["status"] == "completed"
            and row["net_return"] > 0 and row["max_drawdown"] >= -.20
            and row["stale_valuation_days"] == 0]
    pool.sort(key=lambda row: (-row["net_return"], -row["max_drawdown"], row["variant"]))
    return pool[0]["variant"] if pool else None


def _score_feed(feed, scores, variant):
    frame = export_scores(scores[variant])
    return ScoreFeed(feed, frame)


def _period(feed, scored_feeds, config, days, destination, label):
    results = []
    for variant in [*WEIGHTS, "existing_rule_reference"]:
        print(f"{label}: {variant}", flush=True)
        source = feed if variant == "existing_rule_reference" else scored_feeds[variant]
        try:
            result = replay(source, config, days, destination / variant)
            result.update(variant=variant, period=label, status="completed")
        except Exception as error:
            result = {"variant": variant, "period": label, "status": "failed",
                      "error": f"{type(error).__name__}: {error}"}
        results.append(result)
    return results


def report(record, out):
    lines = ["# Public-factor A-share combination replay", "", f"Status: `{record['status']}`.",
             f"Dataset: `{record['dataset']}`", f"Accounts and score artifacts: `{record['artifacts']}`", "",
             "This is a retrospective, research-only comparison on 250 supplier-reported 2023-12-29 main-board members.",
             "The seven public-factor variants share the same BaoStock snapshot, candidate eligibility, cash, costs and trading engine.",
             "The existing-rule reference keeps its own positive-momentum admission filter.",
             "Selection and later check start separate 140,000 RMB accounts; full_period is one continuous account.",
             "The first-period selection was frozen before the later-check and full-period results were calculated.",
             "Neither 2026 segment is a pristine holdout, and no candidate is authorized for paper or live orders.", "",
             "| Period | Variant | Status | Net return | Max drawdown | Fees (RMB) | Turnover (RMB) |",
             "|---|---|---|---:|---:|---:|---:|"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['period']} | {row['variant']} | completed | {row['net_return']:.2%} | "
                         f"{row['max_drawdown']:.2%} | {row['fees']:,.2f} | {row['turnover_value']:,.2f} |")
        else:
            lines.append(f"| {row['period']} | {row['variant']} | {row['status']} | -- | -- | -- | -- |")
    lines += ["", f"Frozen first-period selection: `{record.get('selected') or 'none'}`.",
              f"Passed later research check: `{record.get('research_gate_passed', False)}`.",
              "A favorable row in the full-period table is not a new selection or deployment approval.", "",
              "## Provenance", "",
              "- Microsoft Qlib Alpha158: https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py",
              "- WorldQuant Alpha#101: https://arxiv.org/pdf/1601.00991",
              "- AQR-style momentum inspiration: https://www.aqr.com/Insights/Datasets/Momentum-Indices-Monthly",
              "- MSCI minimum-variance inspiration: https://www.msci.com/indexes/index/719841/msci-china-a-minimum-volatility-index-usd",
              "", "The AQR and MSCI terms are local price-only proxies, not copies of institutional live portfolios.",
              "Point-in-time value/quality fundamentals, independent membership verification, ETFs and BSE remain outside this replay.",
              "Execution uses next-session open, T+1/lot/limit checks and simulated volume participation; queue fills are unknown."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(dataset, out=None, initial_cash=140000):
    dataset = Path(dataset)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(out) if out else ROOT / "outputs" / "institutional_mix" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts = DATA_ROOT / "research" / "institutional_mix" / identifier
    artifacts.mkdir(parents=True, exist_ok=False)
    config = StrategyConfig(initial_cash=initial_cash).validate()
    record = {
        "status": "running", "started_at": datetime.now().astimezone().isoformat(),
        "dataset": str(dataset), "artifacts": str(artifacts), "command": sys.argv,
        "python": sys.executable, "versions": {name: metadata.version(name) for name in ("pyqlib", "pandas", "numpy")},
        "code_hashes": {str(path): sha256(path.read_bytes()).hexdigest() for path in (
            Path(__file__), Path(__file__).with_name("factors.py"), ROOT / "trading/engine.py",
            ROOT / "trading/ledger.py", ROOT / "trading/feed.py", ROOT / "trading/rules.py",
            ROOT / "trading/signals.py", ROOT / "trading/config.py")},
        "manifest_sha256": sha256((dataset / "manifest.json").read_bytes()).hexdigest(),
        "portfolio": config.to_dict(), "weights": WEIGHTS,
        "formulas": formulas().to_dict("records"),
        "periods": {"selection": [START, SELECTION_END], "later_check": [CHECK_START, END]},
        "selection_rule": "highest first-period net return among positive-return candidates with drawdown >= -20% and zero stale valuation days; existing rule excluded",
        "later_check_rule": "selected candidate needs positive later-period return, drawdown >= -15%, zero stale valuation days",
        "signal_time": "same-day close, stamped 15:05 Asia/Shanghai; simulated order no earlier than next session open",
        "comparison": "seven factor variants share 250-stock eligibility and all variants share initial cash, account engine, costs and rebalance; existing-rule reference retains a positive-momentum signal filter",
        "institutional_scope": "Qlib Alpha158 price features; published WorldQuant Alpha#101; AQR-style price momentum proxy; MSCI-style realized low-volatility proxy, not institution production portfolios",
        "fresh_holdout_claimed": False, "strategy_validated": False, "paper_trading_authorized": False,
        "limitations": ["2026 has been inspected in earlier project experiments; later_check is a chronological check, not a pristine holdout",
                        "Supplier-reported 2023-12-29 index membership is not independently verified and excludes many A-shares",
                        "Fixed 250-stock universe has survivorship/membership risk; ETFs and BSE are excluded",
                        "Vendor-adjusted price history may be revised; point-in-time adjustment vintages are not verified",
                        "AQR quality/value and exact MSCI risk model require verified point-in-time fundamentals/constituents",
                        "Simulated fills use next-session open, limits and volume participation, not exchange queue evidence"],
        "results": [],
    }
    write_json(out / "protocol.json", record)
    try:
        feed = HistoricalFeed(dataset)
        selection = feed.manifest.get("selection", {})
        if (feed.manifest.get("scope") != FIXED_SCOPE or not selection.get("snapshot_validated")
                or not selection.get("source_files_complete") or selection.get("missing") != []
                or selection.get("expected_symbols") != 250 or selection.get("complete_symbols") != 250):
            raise ValueError("Requires the validated, complete, fixed pre-period 250-stock snapshot")
        selected_days = [day for day in feed.calendar if START <= day <= SELECTION_END]
        later_days = [day for day in feed.calendar if CHECK_START <= day <= END]
        if not selected_days or not later_days or selected_days[-1] != SELECTION_END or later_days[0] != CHECK_START or later_days[-1] != END:
            raise ValueError("Required 2026 exchange sessions are not complete in the snapshot")
        record["dataset_manifest"] = feed.manifest
        record["input_hashes"] = {name: sha256((feed.folder / name).read_bytes()).hexdigest() for name in (
            "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv", "selection.csv")}
        record["sessions"] = {"selection": len(selected_days), "later_check": len(later_days)}
        write_json(out / "protocol.json", record)
        panel, _, eligible, quality = research_panel(feed, config)
        record["data_quality"] = quality
        native = native_features(panel, feed.calendar, artifacts / "qlib_provider", formulas(), [60])
        scores, coverage = score_matrix(native, feed.raw, eligible)
        record["score_coverage"] = coverage
        scores.to_parquet(artifacts / "scores.parquet")
        scored_feeds = {name: _score_feed(feed, scores, name) for name in WEIGHTS}
        record["results"].extend(_period(feed, scored_feeds, config, selected_days, artifacts / "selection", "selection"))
        pd.DataFrame(record["results"]).to_csv(out / "metrics_selection.csv", index=False)
        chosen = select_on_first_period(record["results"])
        decision = {"selected": chosen, "selection_rows": record["results"],
                    "later_check_feedback_used": False, "frozen_at": datetime.now().astimezone().isoformat()}
        write_json(out / "selection_frozen.json", decision)
        record["selected"] = chosen
        record["results"].extend(_period(feed, scored_feeds, config, later_days, artifacts / "later_check", "later_check"))
        record["results"].extend(_period(feed, scored_feeds, config, selected_days + later_days,
                                         artifacts / "full_period", "full_period_descriptive"))
        if chosen:
            record["full_period_selected"] = next(row for row in record["results"]
                                                  if row["variant"] == chosen and row["period"] == "full_period_descriptive")
            check = next(row for row in record["results"] if row["variant"] == chosen and row["period"] == "later_check")
            record["research_gate_passed"] = bool(check["status"] == "completed" and check["net_return"] > 0
                                                   and check["max_drawdown"] >= -.15 and check["stale_valuation_days"] == 0)
        else:
            record["research_gate_passed"] = False
        record["status"] = ("completed_research_only" if all(row["status"] == "completed" for row in record["results"])
                            else "partial_failure_research_only")
    except Exception as error:
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(out / "protocol.json", record)
        if record["results"]:
            pd.DataFrame(record["results"]).to_csv(out / "metrics.csv", index=False)
            report(record, out)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--initial-cash", type=float, default=140000)
    args = parser.parse_args(argv)
    out = run(args.dataset, args.out, args.initial_cash)
    print(out)
    status = json.loads((out / "protocol.json").read_text(encoding="utf-8"))["status"]
    return 0 if status == "completed_research_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
