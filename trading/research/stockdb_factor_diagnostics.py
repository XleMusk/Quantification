"""Fixed historical value/size/activity signals and calendar-aware research labels."""

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
from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .factor_families import HORIZONS, MIN_DAYS, MIN_PAIRS, forward_open_labels, hac_mean_t, rank_group_means
from .factors import daily_ic
from .price_boundaries import validate_rename
from .stockdb_history_inputs import FEATURES, stock_identities


START, END = "2023-01-01", "2023-12-31"
SCOPES = {"2023_full": (START, END), "2023_h1": (START, "2023-06-30"),
          "2023_h2": ("2023-07-01", END)}
DIRECTIONS = dict(zip(FEATURES, (1, 1, -1, -1)))
BLEND = "value_size_activity_equal_rank_v1"
PRICE_COLUMNS = ["date", "code", "security_id", "tradable_code", "alias_shadow", "adjustflag",
                 "open", "volume", "amount", "tradestatus", "isST"]
LIMITS = [
    "Retrospective 2023 diagnostics, not untouched validation or a current signal",
    "Retrieval-time valuation ratios and listing master are not revision-safe financial PIT or historical membership proof",
    "Saved reconstructed adjusted-open ratios are research labels, not realizable cash/share total returns",
    "Source pre_close and pct_chg are never used in labels; their price-basis conflict remains unresolved",
    "OHLC/activity pair flags are contemporaneous research cohort checks, not independent source certification",
    "Only the already reviewed code rename is resolved; dated source aliases remain in outputs but never both rank",
    "Missing scheduled entry/exit quotes and cross-period exits stay unknown, never zero or a later available quote",
    "Outcome availability does not select the signal pool; missing labels still leave potentially biased observed-outcome means",
    "IC, overlapping horizon spreads and HAC descriptions are not cost-net portfolio PnL, Sharpe or executable alpha",
    "No fitted directions, horizon selection, model training, new ledger/backtester, feed/account mutation or THS operation; NO_TRADE",
]


def identify_source(features, master, rename):
    validate_rename(rename)
    identities = stock_identities(master)
    frame = features.rename(columns={"symbol": "source_symbol"}).copy()
    if not frame.source_symbol.equals(frame.code.map(identities)):
        raise ValueError("Source stock identities no longer match their saved historical master")
    providers = {normalize_symbol(code).vt_symbol: code for code in master.loc[master.type.eq("1"), "code"]}
    frame["source_provider_code"] = frame.source_symbol.map(providers)
    if frame.source_provider_code.isna().any():
        raise ValueError("Unknown source provider identity")
    frame["date"] = pd.to_datetime(frame.date)
    alias = frame.source_provider_code.isin([rename["old"], rename["new"]])
    frame["security_id"] = frame.source_provider_code.replace({rename["new"]: rename["old"]})
    expected = pd.Series(np.where(frame.date.lt(pd.Timestamp(rename["effective_date"])),
                                  rename["old"], rename["new"]), index=frame.index)
    frame["source_is_dated_code"] = ~alias | frame.source_provider_code.eq(expected)
    frame["symbol"] = frame.security_id.map(lambda code: normalize_symbol(code).vt_symbol)
    if frame.loc[frame.source_is_dated_code].duplicated(["date", "security_id"]).any():
        raise ValueError("More than one dated source observation for a security/day")
    return frame


def price_panels(prices, calendar, master, config):
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame.date)
    if (frame.empty or frame.duplicated(["date", "security_id"]).any()
            or not frame.date.isin(calendar).all() or frame.alias_shadow.any()
            or not frame.adjustflag.astype(str).eq("2").all()):
        raise ValueError("Unique canonical adjusted observations on the pinned full calendar required")
    for field in ("open", "volume", "amount", "tradestatus", "isST"):
        frame[field] = pd.to_numeric(frame[field].mask(frame[field].eq("")), errors="raise")
    live = frame.tradestatus.eq(1) & frame.volume.gt(0)
    if (not np.isfinite(frame.loc[live, ["open", "volume", "amount"]]).all().all()
            or frame.loc[live, "open"].le(0).any() or frame.loc[live, "amount"].lt(0).any()):
        raise ValueError("Invalid active canonical research prices/activity")
    frame.loc[~live, "open"] = np.nan
    indexed = frame.set_index(["date", "security_id"]).sort_index()
    opening = indexed.open.unstack("security_id").reindex(calendar)
    amount = indexed.amount.unstack("security_id").reindex(calendar)
    median = amount.rolling(20, min_periods=20).median().stack(future_stack=True)
    median.index = median.index.set_names(["date", "security_id"])
    frame["median_amount_20"] = median.reindex(indexed.index).reindex(
        pd.MultiIndex.from_frame(frame[["date", "security_id"]])).to_numpy()
    instruments = master.set_index("code")
    ipo, retired = frame.security_id.map(instruments.ipoDate), frame.security_id.map(instruments.outDate)
    frame["ipo_date"] = pd.to_datetime(ipo.mask(ipo.eq("")))
    frame["out_date"] = pd.to_datetime(retired.mask(retired.eq("")))
    frame["dated_reference_eligible"] = (live & frame.isST.eq(0)
        & (frame.date - frame.ipo_date).dt.days.ge(config.min_listing_days)
        & (frame.out_date.isna() | frame.date.lt(frame.out_date))
        & frame.median_amount_20.ge(config.min_amount))
    return frame, opening


def join_inputs(features, prices):
    fields = ["date", "security_id", "tradable_code", "median_amount_20", "dated_reference_eligible"]
    joined = features.merge(prices[fields], on=["date", "security_id"], how="left", validate="many_to_one")
    paired = joined.pair_state.eq("both")
    for name in ("ohlc_pair_within_2c", "volume_pair_agrees", "amount_pair_agrees"):
        paired &= joined[name].eq(True).fillna(False)
    joined["decision_eligible"] = (paired & joined.source_is_dated_code
        & joined.dated_reference_eligible.eq(True).fillna(False))
    return joined


def fixed_signals(features):
    oriented = features[list(FEATURES)].mul(pd.Series(DIRECTIONS), axis=1)
    ranks = oriented.groupby(level="date").rank(pct=True)
    # Two value inputs share one family weight, rather than getting two votes.
    complete = ranks.notna().all(axis=1)
    oriented[BLEND] = ((ranks[FEATURES[0]] + ranks[FEATURES[1]]) / 2
                      + ranks[FEATURES[2]] + ranks[FEATURES[3]]) / 3
    oriented.loc[~complete, BLEND] = np.nan
    return oriented


def scoped_labels(labels, exits, start, end):
    days = labels.index.get_level_values("date")
    return labels.where((days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
                        & exits.le(pd.Timestamp(end)).to_numpy())


def summarize(daily):
    rows = []
    for (scope, factor, horizon), part in daily.groupby(["scope", "factor", "horizon"], sort=True):
        ic, excess, spread = part.rank_ic, part.q5_excess, part.q5_minus_q1
        rows.append({"scope": scope, "factor": factor, "horizon": int(horizon),
            "signal_days": int(part.signal_rows.gt(0).sum()), "ic_days": int(ic.count()),
            "signal_rows": int(part.signal_rows.sum()), "paired_labels": int(part.pairs.sum()),
            "missing_labels": int(part.missing_labels.sum()), "mean_rank_ic": ic.mean(),
            "rank_ic_hac_t": hac_mean_t(ic, int(horizon)), "mean_q5_excess": excess.mean(),
            "q5_excess_hac_t": hac_mean_t(excess, int(horizon)), "mean_q5_minus_q1": spread.mean(),
            "descriptive_min_days_met": bool(ic.count() >= MIN_DAYS)})
    return pd.DataFrame(rows)


def run(input_view, price_view):
    input_view, price_view = Path(input_view).resolve(), Path(price_view).resolve()
    source_plan = json.loads((input_view / "plan.json").read_text(encoding="utf-8"))
    source_result = json.loads((input_view / "result.json").read_text(encoding="utf-8"))
    price_result = json.loads((price_view / "result.json").read_text(encoding="utf-8"))
    raw_view = Path(price_result["raw_view"]).resolve()
    if (source_result["status"] != "completed_research_inputs" or source_result["execution_admitted"] is not False
            or price_result["status"] != "research_reconstruction_completed" or price_result["execution_admitted"] is not False
            or Path(source_plan["reference_file"]).resolve() != raw_view / "raw_daily.parquet"):
        raise ValueError("Completed unadmitted inputs tied to the same saved raw reference required")
    months = [f"2023-{month:02d}" for month in range(1, 13)]
    if [part["month"] for part in source_result["months"]] != months:
        raise ValueError("This fixed experiment requires exactly the completed twelve 2023 partitions")
    canonical = (price_view / price_result["canonical_panel_file"]).resolve()
    canonical.relative_to(price_view)
    master = pd.read_csv(source_plan["universe"], dtype=str, keep_default_na=False)
    calendar = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(raw_view / "input_calendar.csv").date))
    if not calendar.is_unique or not calendar.is_monotonic_increasing:
        raise ValueError("Distinct sorted saved exchange calendar required")
    calendar = calendar[(calendar >= pd.Timestamp(START)) & (calendar <= pd.Timestamp(END))]
    config = StrategyConfig()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/stockdb_factor_diagnostics" / identifier
    root.mkdir(parents=True, exist_ok=False)
    (root / "labeled_inputs").mkdir()
    shutil.copy2(__file__, root / "implementation.py")
    plan = {"id": identifier, "created_at": datetime.now().astimezone().isoformat(),
        "input_view": str(input_view), "price_view": str(price_view), "canonical_file": str(canonical),
        "reference_file": source_plan["reference_file"], "universe": source_plan["universe"],
        "scopes": SCOPES, "horizons": HORIZONS, "directions": DIRECTIONS,
        "blend": {"id": BLEND, "formula": "(mean(rank(+B/P),rank(+E/P)) + rank(-log float MV) + rank(-turnover))/3",
                  "missing": "all four inputs required; no imputation or missing-family weight redistribution"},
        "pool": {"min_listing_days": config.min_listing_days, "min_amount_20_session_median": config.min_amount,
                 "rules": "dated live/non-ST reference, dated code identity, paired OHLC <=2c and agreeing activity; no outcome filter"},
        "label": "adjusted O(T+h+1)/O(T+1)-1 on full saved calendar, active endpoints only; purge exits outside scope",
        "min_pairs": MIN_PAIRS, "min_descriptive_days": MIN_DAYS, "hac_lag": "holding horizon",
        "execution_admitted": False, "limits": LIMITS}
    write_json(root / "plan.json", plan)
    result = {"id": identifier, "status": "running", "months": [], "execution_admitted": False,
              "account_ready": False, "source_pre_close_used": False, "models_trained": False}
    write_json(root / "result.json", result)
    try:
        prices = pq.read_table(canonical, columns=PRICE_COLUMNS,
                              filters=[("date", ">=", START), ("date", "<=", END)]).to_pandas()
        prices, opening = price_panels(prices, calendar, master, config)
        features = pd.concat([pd.read_parquet(input_view / "feature_inputs" / (month + ".parquet"))
                              for month in months], ignore_index=True)
        rows = join_inputs(identify_source(features, master, price_result["rename"]), prices)
        row_index = pd.MultiIndex.from_frame(rows[["date", "security_id"]])
        date_series = pd.Series(calendar, index=calendar)
        for horizon in HORIZONS:
            wide = forward_open_labels(opening, horizon)
            labels = wide.stack(future_stack=True)
            labels.index = labels.index.set_names(["date", "security_id"])
            rows[f"label_h{horizon}"] = labels.reindex(row_index).to_numpy()
            rows[f"exit_h{horizon}"] = rows.date.map(date_series.shift(-(horizon + 1)))
        rows["entry_date"] = rows.date.map(date_series.shift(-1))
        if rows.loc[rows.decision_eligible].duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate security in the decision-time research pool")
        eligible = rows.loc[rows.decision_eligible].set_index(["date", "symbol"]).sort_index()
        if eligible.empty:
            raise ValueError("No decision-time research cohort")
        signals = fixed_signals(eligible)
        signals.to_parquet(root / "fixed_signals.parquet")
        for month in months:
            part = rows.loc[rows.date.dt.strftime("%Y-%m").eq(month)]
            part.to_parquet(root / "labeled_inputs" / (month + ".parquet"), index=False)
            check = {"month": month, "rows": len(part), "decision_eligible_rows": int(part.decision_eligible.sum()),
                     "source_alias_rows": int((~part.source_is_dated_code).sum()),
                     "ohlc_conflicted_rows": int(part.ohlc_pair_within_2c.eq(False).fillna(False).sum())}
            result["months"].append(check)
            print(f"{month}: inputs={len(part)}, decision_pool={check['decision_eligible_rows']}", flush=True)
        parts = []
        for scope, (start, end) in SCOPES.items():
            days = signals.index.get_level_values("date")
            score = signals.loc[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
            for horizon in HORIZONS:
                label = scoped_labels(eligible[f"label_h{horizon}"].reindex(score.index),
                                      eligible[f"exit_h{horizon}"].reindex(score.index), start, end)
                ic = daily_ic(score, label, min_pairs=MIN_PAIRS).reset_index().melt(
                    id_vars="date", var_name="factor", value_name="rank_ic")
                groups = rank_group_means(score, label, min_pairs=MIN_PAIRS)
                part = groups.merge(ic, on=["date", "factor"], validate="one_to_one")
                part["q5_minus_q1"] = part.q5_excess - part.q1_excess
                part["scope"], part["horizon"] = scope, horizon
                parts.append(part)
                print(f"{scope} horizon={horizon}: fixed signals={len(score)}, no fitting", flush=True)
        daily = pd.concat(parts, ignore_index=True)
        daily.to_parquet(root / "daily_diagnostics.parquet", index=False)
        summary = summarize(daily)
        summary.to_csv(root / "summary.csv", index=False)
        result.update(status="completed_research_diagnostics", source_rows=len(rows), source_symbols=int(rows.symbol.nunique()),
            decision_eligible_rows=len(eligible), decision_symbols=int(eligible.index.get_level_values("symbol").nunique()),
            signal_sessions=int(eligible.index.get_level_values("date").nunique()), calendar_sessions=len(calendar),
            source_alias_rows=int((~rows.source_is_dated_code).sum()),
            ohlc_conflicted_rows=int(rows.ohlc_pair_within_2c.eq(False).fillna(False).sum()),
            feature_known={name: int(eligible[name].notna().sum()) for name in FEATURES},
            blend_known=int(signals[BLEND].notna().sum()), summaries=summary.to_dict("records"))
    except Exception as error:
        result.update(status="failed_partial_diagnostics", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result["finished_at"] = datetime.now().astimezone().isoformat()
        write_json(root / "result.json", result)
    out = ROOT / "outputs/stockdb_factor_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    lines = ["# Historical Value, Size And Activity Diagnostics", "", "NO_TRADE; retrospective diagnostics, not account PnL.",
             f"Inputs: {input_view}", f"Data: {root}", "",
             f"Source rows: {len(rows)}; decision-eligible rows: {len(eligible)}; securities: {result['decision_symbols']}.",
             f"Calendar sessions: {len(calendar)}; signal sessions: {result['signal_sessions']}.",
             f"Dated source alias rows: {result['source_alias_rows']}; OHLC-conflicted rows: {result['ohlc_conflicted_rows']}; retained in labeled inputs.",
             "", "Directions, horizons and family-equal blend were fixed before the first outcome calculation.",
             "Ranks use the full decision-time feature pool. Missing outcomes do not remove signal candidates.",
             "First 19 sessions have no prior 20-session liquidity median in this saved history; they are not backfilled.",
             "Half-year diagnostics purge exits beyond their own boundary; they are not independent holdouts.", "",
             "| Scope | Signal | Horizon | IC days | Mean rank IC | Q5 excess | Q5-Q1 | Missing labels |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in summary.itertuples():
        lines.append(f"| {row.scope} | {row.factor} | {row.horizon} | {row.ic_days} | {row.mean_rank_ic:.6f} | "
                     f"{row.mean_q5_excess:.6f} | {row.mean_q5_minus_q1:.6f} | {row.missing_labels} |")
    lines += ["", *["- " + limit for limit in LIMITS]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"root": str(root), "report": str(out / "REPORT.md"), "status": result["status"],
                      "decision_eligible_rows": len(eligible), "execution_admitted": False}), flush=True)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-view", type=Path, required=True)
    parser.add_argument("--price-view", type=Path, required=True)
    args = parser.parse_args(argv)
    run(args.input_view, args.price_view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
