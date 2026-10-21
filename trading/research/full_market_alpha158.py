"""Full-market Alpha158 diagnostics on the pinned StockDB research snapshot."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .factor_risk_audit import daily_metrics, no_exit_shock
from .factors import catalog
from .full_market import verify_chunks
from .full_market_mix import (BUY_COST, END, GROSS, HOLDINGS, INITIAL_CASH, MARKET_GROUPS,
                              SELL_COST, SPLIT, load_rows, market_panel, proxy_replay)


FIRST = 20260105
SELECTION_END = 20260630
FORWARD = 5
BATCH = 8


def qcode(code):
    return ("SH" if code.startswith("6") else "BJ" if code.startswith("9") else "SZ") + code


def provider_rows(frame, folder):
    calendar = sorted(frame.date.unique().tolist())
    locations = {day: position for position, day in enumerate(calendar)}
    provider = folder / "qlib_provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    (provider / "features").mkdir()
    (provider / "calendars" / "day.txt").write_text(
        "\n".join(pd.to_datetime(calendar, format="%Y%m%d").strftime("%Y-%m-%d")) + "\n", encoding="utf-8")
    instruments = []
    # Keep source-quality counts alongside the provider artifact.  These are
    # diagnostics only: they do not repair or silently drop source rows.
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    valid_ohlc = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
    valid_ohlc &= prices.high.ge(prices[["open", "low", "close"]].max(axis=1))
    valid_ohlc &= prices.low.le(prices[["open", "high", "close"]].min(axis=1))
    volume_values = pd.to_numeric(frame.volume, errors="coerce")
    amount_values = pd.to_numeric(frame.amount, errors="coerce")
    valid_ohlc &= volume_values.ge(0) & amount_values.ge(0)
    live_mask = prices.close.gt(0) & volume_values.gt(0) & np.isfinite(prices.close.to_numpy(dtype=float))
    vwap_values = amount_values.to_numpy(dtype=float) / np.where(
        volume_values.to_numpy(dtype=float) > 0, volume_values.to_numpy(dtype=float), np.nan)
    finite_vwap = np.isfinite(vwap_values)
    finite_bounds = np.isfinite(prices.low.to_numpy(dtype=float)) & np.isfinite(prices.high.to_numpy(dtype=float))
    vwap_outside = live_mask.to_numpy() & finite_vwap & finite_bounds & (
        (vwap_values < prices.low.to_numpy(dtype=float) * .99)
        | (vwap_values > prices.high.to_numpy(dtype=float) * 1.01))
    quality = {
        "source_rows": int(len(frame)),
        "source_live_rows": int(live_mask.sum()),
        "source_rows_excluded_from_provider": int((~live_mask).sum()),
        "invalid_ohlc_rows": int((~valid_ohlc).sum()),
        "live_invalid_ohlc_rows": int((live_mask & ~valid_ohlc.to_numpy()).sum()),
        "nonpositive_volume_rows": int(volume_values.le(0).sum()),
        "vwap_nonfinite_live_rows": int((live_mask.to_numpy() & ~finite_vwap).sum()),
        "vwap_outside_1pct_ohlc_rows": int(vwap_outside.sum()),
    }
    invalid_vwap = 0
    live_rows = 0
    for code, rows in frame.groupby("code", sort=True):
        rows = rows.sort_values("date")
        positions = np.fromiter((locations[day] for day in rows.date), dtype=np.int32, count=len(rows))
        close = rows.close.to_numpy(dtype=float)
        volume = rows.volume.to_numpy(dtype=float)
        returns = rows.pct_chg.to_numpy(dtype=float) / 100
        if not np.isfinite(returns).all() or (returns < -1).any():
            raise ValueError(f"Invalid provider returns for {code}")
        index = np.cumprod(1 + returns)
        live = (close > 0) & (volume > 0) & np.isfinite(close)
        vwap = rows.amount.to_numpy(dtype=float) / np.where(volume > 0, volume, np.nan)
        invalid_vwap += int((live & np.isfinite(vwap) & (
            (vwap < rows.low.to_numpy(dtype=float) * .99)
            | (vwap > rows.high.to_numpy(dtype=float) * 1.01))).sum())
        live_rows += int(live.sum())
        values = {
            field: index * rows[field].to_numpy(dtype=float) / close
            for field in ("open", "high", "low")
        }
        values.update(close=index, volume=volume, vwap=index * vwap / close, factor=np.ones(len(rows)))
        target = provider / "features" / qcode(code).lower()
        target.mkdir()
        for field, source in values.items():
            column = np.full(len(calendar), np.nan, dtype="<f4")
            column[positions[live]] = source[live]
            np.r_[np.float32(0), column].astype("<f4").tofile(target / f"{field}.day.bin")
        instruments.append(f"{qcode(code)}\t{pd.Timestamp(str(calendar[0])).date()}\t"
                           f"{pd.Timestamp(str(calendar[-1])).date()}\n")
    (provider / "instruments" / "all.txt").write_text("".join(instruments), encoding="utf-8")
    # Cross-check the per-symbol loop against the frame-level counts so a
    # future change cannot make the receipt report a different live-row basis.
    if live_rows != quality["source_live_rows"] or invalid_vwap != quality["vwap_outside_1pct_ohlc_rows"]:
        raise ValueError("Provider input-quality count mismatch")
    return provider, calendar, {"codes": len(instruments), **quality}


def decision_dates(calendar, first=FIRST, split=SPLIT, end=END, forward=FORWARD):
    days = [day for day in calendar if first <= day <= end]
    positions = {day: i for i, day in enumerate(calendar)}
    return [(day, "selection" if day < split else "later_check") for day in days[::forward]
            if positions[day] + forward < len(calendar)
            and (calendar[positions[day] + forward] <= end if day < split else True)]


def evaluate_both(score, label, allowed):
    sample = pd.DataFrame({"score": score, "label": label})
    sample = sample.loc[allowed.reindex(sample.index, fill_value=False) & sample.score.notna()]
    missing = int(sample.label.isna().sum())
    sample = sample.dropna()
    pairs = len(sample)
    if pairs < 100 or sample.score.nunique() < 3:
        return [("high", pairs, missing, None, None), ("low", pairs, missing, None, None)]
    ic = sample.score.corr(sample.label, method="spearman")
    top = sample.score.quantile(.9)
    bottom = sample.score.quantile(.1)
    mean = sample.label.mean()
    return [("high", pairs, missing, float(ic), float(sample.loc[sample.score.ge(top), "label"].mean() - mean)),
            ("low", pairs, missing, float(-ic), float(sample.loc[sample.score.le(bottom), "label"].mean() - mean))]


def rank_summary(daily):
    result = daily.groupby(["factor", "direction", "segment"], sort=False).agg(
        decision_dates=("date", "size"), evaluated_dates=("rank_ic", "count"),
        mean_rank_ic=("rank_ic", "mean"), mean_top_minus_universe=("top_minus_universe", "mean"),
        min_pairs=("pairs", "min"), missing_forward_labels=("missing_forward_labels", "sum"))
    return result.reset_index()


def select_candidates(summary):
    selection = summary.loc[summary.segment.eq("selection")].copy()
    selection = selection.loc[(selection.evaluated_dates >= 10) & (selection.min_pairs >= 100)
                              & selection.mean_rank_ic.gt(0) & selection.mean_top_minus_universe.gt(0)]
    selection = selection.sort_values(["mean_rank_ic", "factor", "direction"], ascending=[False, True, True])
    return selection.drop_duplicates("factor").head(5)[["factor", "direction"]].to_dict("records")


def fetch_features(provider, symbols, formulas, names, start, end):
    from qlib.data import D

    data = D.features(symbols, formulas,
                      start_time=str(pd.to_datetime(start, format="%Y%m%d").date()),
                      end_time=str(pd.to_datetime(end, format="%Y%m%d").date()))
    data.columns = names
    data = data.reset_index().rename(columns={"datetime": "date", "instrument": "symbol"})
    data["date"] = data.date.dt.strftime("%Y%m%d").astype(int)
    data["code"] = data.symbol.str[2:]
    return data


def run(snapshot, group, out=None, batch_size=BATCH, first=FIRST, split=SPLIT, end=END,
        forward=FORWARD):
    import qlib

    snapshot = Path(snapshot)
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    verify_chunks(snapshot, manifest)
    group_config = {name: (prefixes, min_amount, name == "bse_equities")
                    for name, prefixes, min_amount in MARKET_GROUPS}
    if group not in group_config or batch_size < 1:
        raise ValueError("Unknown market group or invalid batch size")
    prefixes, min_amount, bse = group_config[group]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8] + "_" + group
    out = Path(out) if out else ROOT / "outputs" / "full_market_alpha158" / identifier
    artifacts = DATA_ROOT / "research" / "full_market_alpha158" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    definitions = catalog()
    definitions.to_csv(out / "alpha158_catalog.csv", index=False)
    record = {"status": "running", "snapshot": str(snapshot), "snapshot_manifest_sha256": sha256(manifest_bytes).hexdigest(),
              "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "group": group,
              "analysis_start": int(first), "selection_split": int(split),
              "analysis_end": int(end), "forward_sessions": int(forward),
              "definitions": len(definitions), "completed_definitions": 0, "batch_size": batch_size,
              "artifacts": str(artifacts), "source": "StockDB_provider_pct_chg_research_proxy",
              "account_backtest": False, "trading_authorized": False, "fresh_holdout_claimed": False}
    write_json(out / "protocol.json", record)
    try:
        frame = load_rows(snapshot, manifest, prefixes)
        provider, calendar, source = provider_rows(frame, artifacts)
        _, eligible, pct, tradable, buyable, sellable, coverage = market_panel(frame, min_amount, bse=bse)
        stress_source = frame.loc[frame.date.between(first, end) & frame.volume.gt(0) & frame.is_st.eq(False)]
        stress_days = stress_source.groupby("date").pct_chg.median().nsmallest(10).index.tolist()
        del frame
        record.update(source_coverage=source, panel_coverage=coverage, stress_days=stress_days)
        write_json(out / "protocol.json", record)
        index = (1 + pct.fillna(0)).cumprod().where(pct.notna())
        label = index.shift(-forward) / index - 1
        decisions = decision_dates(calendar, first, split, end, forward)
        symbols = [qcode(code) for code in pct.columns]
        qlib.init(provider_uri=str(provider), region="cn", expression_cache=None, dataset_cache=None,
                  joblib_backend="threading", kernels=2)
        rows = []
        for start in range(0, len(definitions), batch_size):
            part = definitions.iloc[start:start + batch_size]
            feature = fetch_features(provider, symbols, part.formula.tolist(), part.name.tolist(), first, end)
            feature = feature.set_index(["date", "code"])
            for day, segment in decisions:
                date_values = feature.loc[day]
                for name in part.name:
                    for direction, pairs, missing, ic, spread in evaluate_both(
                            date_values[name], label.loc[day], eligible.loc[day]):
                        rows.append({"group": group, "date": day, "segment": segment, "factor": name,
                                     "direction": direction, "eligible": int(eligible.loc[day].sum()),
                                     "pairs": pairs, "missing_forward_labels": missing,
                                     "rank_ic": ic, "top_minus_universe": spread})
            record["completed_definitions"] = min(start + batch_size, len(definitions))
            pd.DataFrame(rows).to_csv(out / "daily_factor_diagnostics.csv", index=False)
            write_json(out / "protocol.json", record)
            print(f"{group}: {record['completed_definitions']}/{len(definitions)}", flush=True)
        daily = pd.DataFrame(rows)
        summary = rank_summary(daily)
        summary.to_csv(out / "factor_summary.csv", index=False)
        chosen = select_candidates(summary)
        record["selection_candidates"] = chosen
        replay_rows = []
        if chosen:
            chosen_defs = definitions.set_index("name").loc[[item["factor"] for item in chosen]]
            scores = fetch_features(provider, symbols, chosen_defs.formula.tolist(), chosen_defs.index.tolist(), first, end)
            scores = scores.set_index(["date", "code"])
            sessions = pct.index[(pct.index >= first) & (pct.index <= end)].tolist()
            decision_grid = set(sessions[::forward])
            selections = {}
            for day in sorted(decision_grid):
                day_scores = scores.loc[day]
                allowed = eligible.loc[day]
                selections[day] = {}
                for item in chosen:
                    key = item["factor"] + "_" + item["direction"]
                    score = day_scores[item["factor"]]
                    if item["direction"] == "low":
                        score = -score
                    sample = score.loc[allowed.reindex(score.index, fill_value=False)].dropna()
                    if len(sample) < 100:
                        raise ValueError(f"Insufficient candidate coverage on {day}: {key}")
                    selections[day][key] = sample.sort_values(ascending=False, kind="stable").head(HOLDINGS).index.tolist()
            periods = {"selection": [day for day in sessions if day < split],
                       "later_check": [day for day in sessions if day >= split], "full_period": sessions}
            for item in chosen:
                key = item["factor"] + "_" + item["direction"]
                for period, dates in periods.items():
                    selected = {day: picks for day, picks in selections.items() if day in set(dates)}
                    result, marks = proxy_replay(dates, selected, pct, tradable, key,
                                                 BUY_COST, SELL_COST, buyable, sellable)
                    marks.to_csv(out / f"{key}_{period}_daily.csv", index=False)
                    metrics = daily_metrics(marks)
                    day_returns = marks.set_index("date").equity.pct_change().fillna(0)
                    stressed = day_returns.reindex(stress_days).dropna() if period == "full_period" else pd.Series(dtype=float)
                    replay_rows.append({"group": group, "variant": key, "period": period,
                                        **metrics, "cost_haircut_cny": result["cost_haircut_cny"],
                                        "approximate_queue_vetoes": result["approximate_queue_vetoes"],
                                        "worst_observed_market_day_return": float(stressed.min()) if not stressed.empty else None})
            pd.DataFrame(replay_rows).to_csv(out / "candidate_proxy_metrics.csv", index=False)
        record.update(status="completed_research_only", completed_definitions=len(definitions),
                      factor_orientations=len(summary) // 2, decision_dates=len(decisions),
                      no_exit_80pct_gross_two_day_20pct_loss=no_exit_shock(GROSS, -.20, 2),
                      candidate_proxy_replays=len(replay_rows))
        lines = ["# Full-market Alpha158 research batch", "",
                 f"Group: `{group}`; observed codes: {source['codes']:,}; source rows: {coverage['source_rows']:,}.",
                 f"Qlib Alpha158 definitions completed: **{len(definitions)}/158**; decision dates: {len(decisions)}.",
                 f"Candidate orientations selected on the first segment: {len(chosen)}; proxy period runs: {len(replay_rows)}.",
                 "", "All prices and five-session labels use the StockDB provider-return research proxy.",
                 "The top-five, 80%-gross replays are non-executable approximations, not engine/ledger account returns.",
                 "The 2026 outcomes have been inspected in prior work; the later segment is not a fresh holdout.",
                 f"Two consecutive -20% held-basket sessions with no exit imply {no_exit_shock(GROSS, -.20, 2):.1%} incremental loss.",
                 "No strategy is promoted to paper or live orders.", "", "## Selected candidates", ""]
        if chosen:
            for item in chosen:
                key = item["factor"] + "_" + item["direction"]
                later = next(row for row in replay_rows if row["variant"] == key and row["period"] == "later_check")
                full = next(row for row in replay_rows if row["variant"] == key and row["period"] == "full_period")
                lines.append(f"- {key}: full Sharpe {full['sharpe_zero_rf_252']:.2f}, later Sharpe "
                             f"{later['sharpe_zero_rf_252']:.2f}, full return {full['return']:.2%}, "
                             f"max drawdown {full['max_drawdown']:.2%}, worst day {full['worst_day']:.2%}.")
        else:
            lines.append("No first-segment factor passed the exploratory positive-IC/spread screen.")
        lines += ["", "See `alpha158_catalog.csv`, `factor_summary.csv`, `daily_factor_diagnostics.csv`, "
                  "`candidate_proxy_metrics.csv` when present, and `protocol.json` for exact coverage.",
                  "VWAP/volume and historical membership remain source-quality risks; the ETF and WorldQuant101 "
                  "families are separate unfinished work."]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(out / "protocol.json", record)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--group", choices=[name for name, *_ in MARKET_GROUPS], required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--batch-size", type=int, default=BATCH)
    parser.add_argument("--first", type=int, default=FIRST)
    parser.add_argument("--split", type=int, default=SPLIT)
    parser.add_argument("--end", type=int, default=END)
    parser.add_argument("--forward", type=int, default=FORWARD)
    args = parser.parse_args(argv)
    print(run(args.snapshot, args.group, args.out, args.batch_size,
              args.first, args.split, args.end, args.forward))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
