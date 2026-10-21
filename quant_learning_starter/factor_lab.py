"""Calendar-aligned calculations for synthetic education, not a trading engine."""

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

FACTORS = ("reversal_5", "momentum_20", "relative_volume_20")
LABEL = "forward_open_return_5"


def synthetic_panel(seed=20260909, sessions=180, stocks=24):
    """Independent random innovations; no planted predictive factor."""
    if sessions < 30 or stocks < 5:
        raise ValueError("Use at least 30 sessions and 5 stocks for this tutorial")
    rng = np.random.default_rng(seed)
    weekdays = pd.bdate_range("2025-01-06", periods=sessions + 2)
    # Two invented closures exercise session indexing, not any exchange calendar.
    calendar = weekdays.delete([12, 27]).rename("date")
    market = rng.normal(0, 0.004, sessions)
    frames = []
    for number in range(stocks):
        overnight = rng.normal(0, 0.005, sessions)
        intraday = market + rng.normal(0, 0.012, sessions)
        close = (20 + number) * np.exp(np.cumsum(overnight + intraday))
        opening = close / np.exp(intraday)
        spread = rng.uniform(0.001, 0.015, sessions)
        frames.append(pd.DataFrame({
            "date": calendar,
            "symbol": f"SYN{number:03d}",
            "open": opening,
            "high": np.maximum(opening, close) * (1 + spread),
            "low": np.minimum(opening, close) * (1 - spread),
            "close": close,
            "volume": rng.lognormal(12, 0.35, sessions).astype("int64"),
            "source": "synthetic",
        }))
    panel = pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])
    # Drop a few observations after warm-up so missing-session handling is visible.
    missing = (rng.random(len(panel)) < 0.01) & (panel["date"] > calendar[30])
    return panel.loc[~missing].reset_index(drop=True), calendar


def compute_factors_and_labels(panel, calendar):
    """Signal after close t; entry open t+1; exit open t+6 (five sessions)."""
    calendar = pd.DatetimeIndex(calendar, name="date")
    if calendar.hasnans or not calendar.is_unique or not calendar.is_monotonic_increasing:
        raise ValueError("Calendar must contain unique, sorted, non-missing sessions")
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    if panel.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate date/symbol observations")
    if not panel["date"].isin(calendar).all():
        raise ValueError("Panel contains dates outside the supplied calendar")
    symbols = sorted(panel["symbol"].unique())
    indexed = panel.set_index(["date", "symbol"])
    wide = {}
    for field in ("open", "close", "volume"):
        values = indexed[field].unstack("symbol").reindex(index=calendar, columns=symbols)
        wide[field] = values.where(np.isfinite(values) & (values >= 0 if field == "volume" else values > 0))
    close, opening, volume = wide["close"], wide["open"], wide["volume"]
    volume_mean = volume.shift(1).rolling(20, min_periods=20).mean()
    calculations = {
        "reversal_5": -(close / close.shift(5) - 1),
        "momentum_20": close / close.shift(20) - 1,
        "relative_volume_20": volume / volume_mean.where(volume_mean > 0),
        LABEL: (opening.shift(-6) / opening.shift(-1) - 1).where(close.notna()),
    }
    # Shifts operate on the full supplied calendar; missing stock rows stay missing.
    result = pd.DataFrame({
        name: values.where(close.notna()).stack(future_stack=True)
        for name, values in calculations.items()
    }).reset_index()
    dates = pd.Series(calendar, index=calendar)
    result["entry_date"] = result["date"].map(dates.shift(-1))
    result["exit_date"] = result["date"].map(dates.shift(-6))
    return result


def evaluate_cross_sections(frame, min_pairs=5, quantiles=5):
    """Spearman IC and date-equal group diagnostics, without PnL compounding."""
    daily, groups = [], []
    for date, cross_section in frame.groupby("date", sort=True):
        for factor in FACTORS:
            values = cross_section[["symbol", factor, LABEL]].replace([np.inf, -np.inf], np.nan)
            paired = values.dropna(subset=[factor, LABEL])
            reason, ic = "ok", np.nan
            if len(paired) < min_pairs:
                reason = "insufficient_pairs"
            elif paired[factor].nunique() < 2:
                reason = "constant_factor"
            elif paired[LABEL].nunique() < 2:
                reason = "constant_label"
            else:
                ic = float(spearmanr(paired[factor], paired[LABEL]).statistic)

            # Group membership uses only information available at signal time.
            signals = values.dropna(subset=[factor]).copy()
            group_reason = "ok"
            if len(signals) < max(min_pairs, quantiles):
                group_reason = "insufficient_signals"
            elif signals[factor].nunique() < 2:
                group_reason = "constant_factor"
            else:
                signals["group"] = pd.qcut(signals[factor], quantiles, labels=False, duplicates="drop") + 1
                if signals["group"].nunique() != quantiles:
                    group_reason = "insufficient_distinct_bins"
                else:
                    for group, members in signals.groupby("group", sort=True):
                        groups.append({
                            "date": date, "factor": factor, "group": int(group),
                            "n_signals": len(members), "n_observed": int(members[LABEL].count()),
                            "mean_forward_return": members[LABEL].mean(),
                        })
            daily.append({
                "date": date, "factor": factor, "n_signals": len(signals),
                "n_pairs": len(paired), "rank_ic": ic,
                "ic_reason": reason, "group_reason": group_reason,
            })
    return pd.DataFrame(daily), pd.DataFrame(groups, columns=[
        "date", "factor", "group", "n_signals", "n_observed", "mean_forward_return",
    ])


def summarize(daily, groups):
    rows = []
    for factor in FACTORS:
        subset = daily.loc[daily["factor"] == factor]
        row = {
            "factor": factor,
            "valid_ic_days": int(subset["rank_ic"].count()),
            "mean_rank_ic": subset["rank_ic"].mean(),
            "std_rank_ic": subset["rank_ic"].std(),
        }
        for group in range(1, 6):
            group_values = groups.loc[(groups["factor"] == factor) & (groups["group"] == group)]
            row[f"q{group}_mean_label"] = group_values["mean_forward_return"].mean()
        rows.append(row)
    return pd.DataFrame(rows)


def manual_example():
    calendar = pd.bdate_range("2025-01-06", periods=30, name="date")
    panel = pd.DataFrame({
        "date": calendar, "symbol": "HAND",
        "open": np.arange(100.0, 130), "close": np.arange(102.0, 132),
        "volume": np.arange(1000.0, 1300, 10),
    })
    computed = compute_factors_and_labels(panel, calendar).iloc[20]
    expected = {
        "reversal_5": -(122 / 117 - 1),
        "momentum_20": 122 / 102 - 1,
        "relative_volume_20": 1200 / 1095,
        LABEL: 126 / 121 - 1,
    }
    checks = pd.DataFrame({"expected": expected, "actual": computed[list(expected)]}).astype(float)
    checks["absolute_error"] = (checks["actual"] - checks["expected"]).abs()
    return checks, computed[["date", "entry_date", "exit_date"]]
