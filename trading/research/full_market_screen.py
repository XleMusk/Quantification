"""Broad-universe signal diagnostics only; no account returns or orders."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .full_market import verify_chunks


START, END = 20250924, 20260924
SPLIT = 20260401
FAMILIES = ("momentum_20", "momentum_60_skip5", "momentum_120_skip5",
            "breakout_20", "reversal_5", "low_volatility_20", "risk_adjusted_momentum",
            "trend_defensive", "trend_breakout", "trend_reversal")


def load_group(snapshot, prefixes):
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    verify_chunks(snapshot, manifest)
    parts = []
    for item in manifest["chunks"]:
        if item["prefix"] in prefixes:
            parts.append(pd.read_parquet(snapshot / item["parquet_file"],
                                         columns=["code", "date", "close", "amount", "volume", "pct_chg", "is_st"]))
    frame = pd.concat(parts, ignore_index=True)
    if frame.duplicated(["date", "code"]).any():
        raise ValueError("Duplicate source rows across chunks")
    return frame


def features(index):
    daily = index.pct_change(fill_method=None)
    momentum_20 = index / index.shift(20) - 1
    momentum_60 = index.shift(5) / index.shift(60) - 1
    momentum_120 = index.shift(5) / index.shift(120) - 1
    prior_high = index.shift(1).rolling(20, min_periods=20).max()
    breakout_20 = index / prior_high - 1
    reversal_5 = -(index / index.shift(5) - 1)
    low_vol = -daily.rolling(20, min_periods=20).std()
    risk_adjusted = momentum_60 / (-low_vol).replace(0, np.nan)
    return {"momentum_20": momentum_20, "momentum_60_skip5": momentum_60,
            "momentum_120_skip5": momentum_120, "breakout_20": breakout_20,
            "reversal_5": reversal_5, "low_volatility_20": low_vol,
            "risk_adjusted_momentum": risk_adjusted}


def combined(scores):
    ranks = {name: score.rank(pct=True) for name, score in scores.items()}
    return {"trend_defensive": .6 * ranks["momentum_60_skip5"] + .4 * ranks["low_volatility_20"],
            "trend_breakout": .5 * ranks["momentum_60_skip5"] + .5 * ranks["breakout_20"],
            "trend_reversal": .5 * ranks["momentum_120_skip5"] + .5 * ranks["reversal_5"]}


def evaluate(score, label, eligibility, minimum=100):
    sample = pd.DataFrame({"score": score, "forward_5_session_return": label})
    sample = sample.loc[eligibility & sample.score.notna()]
    missing_label = int(sample.forward_5_session_return.isna().sum())
    sample = sample.dropna()
    if len(sample) < minimum or sample.score.nunique() < 3:
        return {"pairs": len(sample), "missing_forward_labels": missing_label,
                "rank_ic": None, "top_decile_mean_forward_return": None,
                "universe_mean_forward_return": None, "top_minus_universe": None}
    rank_ic = sample.score.corr(sample.forward_5_session_return, method="spearman")
    threshold = sample.score.quantile(.9)
    top = sample.loc[sample.score.ge(threshold), "forward_5_session_return"]
    baseline = sample.forward_5_session_return.mean()
    return {"pairs": len(sample), "missing_forward_labels": missing_label,
            "rank_ic": float(rank_ic) if np.isfinite(rank_ic) else None,
            "top_decile_mean_forward_return": float(top.mean()),
            "universe_mean_forward_return": float(baseline),
            "top_minus_universe": float(top.mean() - baseline)}


def diagnostic(frame, group, min_amount):
    panel = frame.pivot(index="date", columns="code", values="close").sort_index()
    percent = frame.pivot(index="date", columns="code", values="pct_chg").reindex_like(panel)
    if (percent.isna() & panel.notna()).any().any():
        raise ValueError(f"{group}: provider percentage change is incomplete")
    # Provider pct_chg agrees with BaoStock on the audited paired sample. This
    # index is a research feature/label proxy, not a cash-dividend account path.
    index = (1 + percent / 100).fillna(1).cumprod().where(panel.notna())
    liquidity = frame.pivot(index="date", columns="code", values="amount").reindex_like(panel)
    volume = frame.pivot(index="date", columns="code", values="volume").reindex_like(panel)
    is_st = frame.pivot(index="date", columns="code", values="is_st").reindex_like(panel)
    observed = panel.notna().cumsum()
    eligible = panel.notna() & observed.ge(120) & volume.gt(0) & liquidity.ge(min_amount) & is_st.eq(False)
    label = index.shift(-5) / index - 1
    calendar = panel.index.tolist()
    dates = [day for offset, day in enumerate(calendar) if START <= day <= END and offset + 5 < len(calendar)]
    decisions = dates[::5]
    singles = features(index)
    rows = []
    for day in decisions:
        daily_scores = {name: value.loc[day] for name, value in singles.items()}
        daily_scores.update(combined(daily_scores))
        for family in FAMILIES:
            result = evaluate(daily_scores[family], label.loc[day], eligible.loc[day])
            rows.append({"group": group, "date": day, "segment": "selection" if day < SPLIT else "later_check",
                         "family": family, "eligible_symbols": int(eligible.loc[day].sum()), **result})
    return pd.DataFrame(rows), {"group": group, "codes": panel.shape[1], "sessions": panel.shape[0],
                                "decision_dates": len(decisions), "last_decision_date": decisions[-1] if decisions else None,
                                "return_basis": "provider_pct_chg_proxy",
                                "execution_admitted": False}


def summarize(daily):
    grouped = daily.groupby(["group", "segment", "family"], sort=False)
    result = grouped.agg(decision_dates=("date", "size"), evaluated_dates=("rank_ic", "count"),
                         mean_rank_ic=("rank_ic", "mean"),
                         positive_rank_ic_fraction=("rank_ic", lambda value: float(value.gt(0).mean())),
                         mean_top_minus_universe=("top_minus_universe", "mean"),
                         min_pairs=("pairs", "min"), missing_forward_labels=("missing_forward_labels", "sum"))
    return result.reset_index().sort_values(["group", "segment", "mean_rank_ic"], ascending=[True, True, False])


def run(snapshot, out=None):
    snapshot = Path(snapshot)
    out = Path(out) if out else ROOT / "outputs" / "full_market" / snapshot.name / "screen"
    out.mkdir(parents=True, exist_ok=False)
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    protocol = {"status": "running", "created_at": datetime.now().astimezone().isoformat(),
                "snapshot": str(snapshot), "manifest_sha256": sha256(manifest_bytes).hexdigest(),
                "screen_code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                "start": START, "end": END, "later_check_start": SPLIT,
                "decision_cadence": "every fifth exchange session beginning 2025-09-24",
                "label": "next five global exchange sessions, proxy adjusted by provider pct_chg",
                "families_predeclared": FAMILIES,
                "groups": {"sh_sz_equities": ["0", "3", "6"], "bse_equities": ["9"]},
                "eligibility": "at least 120 observed bars, positive volume, not ST, amount >= 20m SH/SZ or 5m BSE",
                "fund_code_group": "not screened: ETF/LOF identity and 2024-2026 adjusted returns unverified",
                "account_backtest": False, "commission_or_slippage_applied": False,
                "strategy_validated": False, "fresh_holdout_claimed": False,
                "limitations": ["No executable entry/exit prices, limit queues, account ledger or drawdown",
                                "Missing delisted codes and forward labels can bias the screen",
                                "Earlier 2026 results were inspected in this project; later_check is not untouched",
                                "Signal rank IC/top-decile diagnostics are not portfolio net returns"]}
    write_json(out / "protocol.json", protocol)
    try:
        parts, group_info = [], []
        for group, prefixes, amount in (("sh_sz_equities", {"0", "3", "6"}, 20_000_000),
                                        ("bse_equities", {"9"}, 5_000_000)):
            frame = load_group(snapshot, prefixes)
            daily, info = diagnostic(frame, group, amount)
            parts.append(daily)
            group_info.append(info)
            print(f"{group}: {info['codes']} codes, {info['decision_dates']} decision dates", flush=True)
        daily = pd.concat(parts, ignore_index=True)
        daily.to_csv(out / "daily_factor_diagnostics.csv", index=False)
        summarize(daily).to_csv(out / "factor_summary.csv", index=False)
        protocol["status"] = "complete_signal_diagnostics_only"
        protocol["group_info"] = group_info
    except Exception as error:
        protocol["status"] = "failed_signal_diagnostics"
        protocol["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(out / "protocol.json", protocol)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    print(run(args.snapshot, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
