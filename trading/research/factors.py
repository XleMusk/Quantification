"""Native Alpha158, bounded templates and date-wise panel diagnostics."""
import re

import numpy as np
import pandas as pd

from quant_learning_starter.factor_lab import LABEL, compute_factors_and_labels


def catalog():
    from qlib.contrib.data.loader import Alpha158DL
    formulas, names = Alpha158DL.get_feature_config()
    if len(names) != 158 or len(set(names)) != 158:
        raise ValueError("Expected the installed default Alpha158 definition")
    rows = []
    for name, formula in zip(names, formulas):
        family = re.sub(r"\d+$", "", name)
        if family.startswith("K"):
            group = "candle_structure"
        elif family in {"OPEN", "HIGH", "LOW", "VWAP"}:
            group = "relative_price"
        elif family in {"VMA", "VSTD", "WVMA", "VSUMP", "VSUMN", "VSUMD"}:
            group = "volume_structure"
        elif family in {"CORR", "CORD"}:
            group = "price_volume_relationship"
        elif family == "STD":
            group = "price_level_dispersion_not_return_volatility"
        else:
            group = "trend_reversal_range_position"
        note = "Normalized time trend slope, NOT market beta" if family == "BETA" else ""
        rows.append({"name": name, "family": family, "category": group, "formula": formula, "note": note})
    return pd.DataFrame(rows)


def research_eligibility(feed, config):
    """Common dated stock research pool; not account/order eligibility."""
    raw = feed.raw.set_index(["date", "symbol"]).sort_index()
    rows = raw.reset_index()
    rows["date"] = pd.to_datetime(rows.date)
    amount = raw.amount.unstack().reindex(feed.calendar).rolling(20, min_periods=20).median().stack(future_stack=True)
    amount.index = amount.index.set_names(["date", "symbol"])
    amount.index = pd.MultiIndex.from_arrays([pd.to_datetime(amount.index.get_level_values(0)), amount.index.get_level_values(1)], names=["date", "symbol"])
    rows["ipo"] = rows.symbol.map({s: pd.Timestamp(i["ipoDate"]) for s, i in feed.instruments.items()})
    rows["out"] = rows.symbol.map({s: pd.Timestamp(i["outDate"]) if i.get("outDate") else pd.NaT for s, i in feed.instruments.items()})
    eligible = ((rows.tradestatus == 1) & (rows.isST == 0) & (rows.volume > 0)
                & ((rows.date - rows.ipo).dt.days >= config.min_listing_days)
                & (rows.out.isna() | (rows.date < rows.out))
                & (rows.date > pd.Timestamp(feed.manifest["selection_date"])))
    eligible.index = pd.MultiIndex.from_frame(rows[["date", "symbol"]])
    return eligible & amount.ge(config.min_amount).reindex(eligible.index, fill_value=False)


def normalized_prices(raw, adjusted):
    """Common research-only price/volume normalization, without feed admission."""
    if raw.index.has_duplicates or adjusted.index.has_duplicates or raw.empty:
        raise ValueError("Nonempty unique raw/adjusted research identities required")
    if not raw.index.equals(adjusted.index) or not adjusted.adjustflag.astype(str).eq("2").all():
        raise ValueError("Raw/adjusted observations must align exactly with declared adjustment flags")
    if not raw.tradestatus.isin([0, 1]).all():
        raise ValueError("Explicit valid raw trade status required")
    activity = raw.loc[raw.tradestatus.eq(1), ["volume", "amount"]]
    if not np.isfinite(activity).all().all() or activity.lt(0).any().any():
        raise ValueError("Invalid active raw volume/amount cannot be masked as a halt")
    live = raw.tradestatus.eq(1) & raw.volume.gt(0)
    prices = adjusted[["open", "high", "low", "close"]]
    if not np.isfinite(prices.loc[live]).all().all() or prices.loc[live].le(0).any().any():
        raise ValueError("Invalid adjusted prices")
    if (prices.loc[live].high + 1e-7 < prices.loc[live].max(axis=1)).any() or (prices.loc[live].low - 1e-7 > prices.loc[live].min(axis=1)).any():
        raise ValueError("Inconsistent adjusted OHLC")
    ratios = prices / raw[["open", "high", "low", "close"]]
    if (ratios.loc[live].max(axis=1) / ratios.loc[live].min(axis=1) - 1).gt(2e-5).any():
        raise ValueError("OHLC adjustments do not share a consistent factor")
    factor = ratios.close
    # Anchoring removes a symbol-wide future adjustment scale from the snapshot.
    # It does not establish point-in-time correctness of vendor revisions.
    factor = factor / factor.groupby(level="symbol").transform("first")
    values = raw[["open", "high", "low", "close"]].mul(factor, axis=0)
    values["volume"] = raw.volume / factor
    values["vwap"] = raw.amount / raw.volume.where(raw.volume.gt(0)) * factor
    values["factor"] = factor
    if not np.isfinite(values.loc[live]).all().all():
        raise ValueError("Missing adjusted volume/VWAP/factor")
    tolerance = raw.high * 2e-5
    raw_vwap = raw.amount / raw.volume.where(raw.volume.gt(0))
    if ((raw_vwap < raw.low - tolerance) | (raw_vwap > raw.high + tolerance)).loc[live].any():
        raise ValueError("Amount/volume units or VWAP are inconsistent with raw OHLC")
    values.loc[~live] = np.nan
    return values, {"raw_rows": len(raw), "suspended_or_zero_volume": int((~live).sum()),
                    "ohlc_adjustment_check": "passed",
                    "volume_unit_check": "amount_RMB / raw_volume_shares lies within raw daily OHLC",
                    "adjustment": "qfq/raw ratio anchored to first observation; price*factor, volume/factor"}


def research_panel(feed, config):
    raw = feed.raw.set_index(["date", "symbol"]).sort_index()
    adjusted = feed.adjusted.set_index(["date", "symbol"]).sort_index()
    values, quality = normalized_prices(raw, adjusted)
    values = values.reset_index()
    values["date"] = pd.to_datetime(values.date)
    labels = compute_factors_and_labels(values, pd.to_datetime(feed.calendar))
    labels = labels.set_index(["date", "symbol"])[[LABEL, "entry_date", "exit_date"]]
    eligible = research_eligibility(feed, config)
    return values, labels, eligible, {**quality, "symbols": len(feed.symbols),
        "eligible_rows": int(eligible.sum()), "universe": feed.manifest["scope"],
        "adjustment": "vendor qfq/raw ratio anchored to first observation; price*factor, volume/factor"}


def qcode(symbol):
    code, market = symbol.split(".")
    return {"SSE": "SH", "SZSE": "SZ"}[market] + code


def write_provider(panel, calendar, folder, *, provider_fields=None):
    """The shared Qlib binary exporter; observed rows do not establish PIT membership."""
    if (panel.empty or panel.duplicated(["date", "symbol"]).any() or not calendar
            or calendar != sorted(set(calendar))
            or not pd.to_datetime(panel.date).isin(pd.to_datetime(calendar)).all()):
        raise ValueError("Unique observed panel within a distinct ordered calendar required")
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "calendars").mkdir()
    (folder / "instruments").mkdir()
    (folder / "calendars/day.txt").write_text("\n".join(calendar) + "\n", encoding="utf-8")
    symbols = sorted(panel.symbol.unique())
    (folder / "instruments/all.txt").write_text("".join(
        f"{qcode(s)}\t{calendar[0]}\t{calendar[-1]}\n" for s in symbols), encoding="utf-8")
    for symbol, rows in panel.groupby("symbol"):
        data = rows.set_index("date").reindex(pd.to_datetime(calendar))
        destination = folder / "features" / qcode(symbol).lower()
        destination.mkdir(parents=True)
        fields = provider_fields if provider_fields is not None else ("open", "high", "low", "close", "volume", "vwap", "factor")
        for field in fields:
            np.r_[0., data[field].to_numpy()].astype("<f4").tofile(destination / f"{field}.day.bin")
    return symbols


def features_from_provider(folder, symbols, calendar, definitions, windows):
    """Use the installed Qlib expression engine for an already prepared provider."""
    from qlib.data import D
    import qlib
    qlib.init(provider_uri=str(folder), region="cn", expression_cache=None, dataset_cache=None,
              joblib_backend="threading", kernels=2)
    extra = [f"Std($close/Ref($close,1)-1,{w})" for w in windows]
    expressions = definitions.formula.tolist() + extra
    frame = D.features([qcode(s) for s in symbols], expressions, start_time=calendar[0], end_time=calendar[-1])
    frame.columns = definitions.name.tolist() + [f"RETVOL{w}" for w in windows]
    frame = frame.reset_index().rename(columns={"datetime": "date", "instrument": "symbol"})
    frame.symbol = frame.symbol.map({qcode(s): s for s in symbols})
    if frame.symbol.isna().any():
        raise ValueError("Unrecognized Qlib instrument")
    return frame.set_index(["date", "symbol"]).sort_index().replace([np.inf, -np.inf], np.nan)


def native_features(panel, calendar, folder, definitions, windows, *, provider_fields=None):
    symbols = write_provider(panel, calendar, folder, provider_fields=provider_fields)
    return features_from_provider(folder, symbols, calendar, definitions, windows)


def candidate_batches(features, windows):
    """Yield the established templates without retaining all 200 full matrices."""
    rank = lambda frame: frame.groupby(level="date").rank(pct=True)
    momenta = rank(pd.DataFrame({w: 1 / features[f"ROC{w}"] - 1 for w in windows}))
    volumes = rank(pd.DataFrame({w: 1 / features[f"VMA{w}"].where(features[f"VMA{w}"] > 0) for w in windows}))
    vols = rank(pd.DataFrame({w: features[f"RETVOL{w}"] for w in windows}))
    positions = rank(pd.DataFrame({w: features[f"RSV{w}"] for w in windows}))
    slopes = rank(pd.DataFrame({w: features[f"BETA{w}"] for w in windows}))
    corrs = rank(pd.DataFrame({w: features[f"CORR{w}"] for w in windows}))
    number = 0
    for w in windows:
        for v in windows:
            family_values = [
                ("trend_volume", momenta[w] * volumes[v], f"CSRank(1/ROC{w}-1)*CSRank(1/VMA{v})"),
                ("trend_risk", momenta[w] / (1 + vols[v]), f"CSRank(1/ROC{w}-1)/(1+CSRank(RETVOL{v}))"),
                ("relative_trend", momenta[w] - momenta[v], f"CSRank(1/ROC{w}-1)-CSRank(1/ROC{v}-1)"),
                ("pullback_trend", momenta[w] * (1 - momenta[v]), f"CSRank(1/ROC{w}-1)*(1-CSRank(1/ROC{v}-1))"),
                ("position_volume", positions[w] * volumes[v], f"CSRank(RSV{w})*CSRank(1/VMA{v})"),
                ("trend_correlation", momenta[w] * corrs[v], f"CSRank(1/ROC{w}-1)*CSRank(CORR{v})"),
                ("slope_risk", slopes[w] / (1 + vols[v]), f"CSRank(BETA{w})/(1+CSRank(RETVOL{v}))"),
                ("quiet_reversal", (1 - momenta[w]) * (1 - volumes[v]), f"(1-CSRank(1/ROC{w}-1))*(1-CSRank(1/VMA{v}))"),
            ]
            records, values = [], {}
            for family, value, formula in family_values:
                name = f"T{number:03d}"
                number += 1
                records.append({"name": name, "family": family, "window_1": w, "window_2": v, "formula": formula})
                values[name] = value
            yield pd.DataFrame(values), pd.DataFrame(records)


def candidates(features, windows):
    batches = list(candidate_batches(features, windows))
    return (pd.concat([values for values, _ in batches], axis=1),
            pd.concat([definitions for _, definitions in batches], ignore_index=True))


def segment_mask(index, labels, start, end, *, require_label=True):
    dates = index.get_level_values("date")
    selected = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    if require_label:
        selected &= labels.exit_date.reindex(index).le(pd.Timestamp(end)).to_numpy()
        selected &= np.isfinite(labels[LABEL].reindex(index).to_numpy())
    return selected


def daily_ic(features, label, min_pairs=5):
    """Rank both variables within each date and each pairwise-finite sample."""
    values = []
    target = label.reindex(features.index)
    for day, rows in features.groupby(level="date", sort=True):
        y = target.loc[rows.index].to_numpy()
        x = rows.to_numpy(dtype=float)
        valid = np.isfinite(x) & np.isfinite(y[:, None])
        rx = pd.DataFrame(np.where(valid, x, np.nan)).rank().to_numpy()
        ry = pd.DataFrame(np.where(valid, y[:, None], np.nan)).rank().to_numpy()
        count = valid.sum(axis=0)
        rx = rx - np.nansum(rx, axis=0) / np.maximum(count, 1)
        ry = ry - np.nansum(ry, axis=0) / np.maximum(count, 1)
        denominator = np.sqrt(np.nansum(rx ** 2, axis=0) * np.nansum(ry ** 2, axis=0))
        ic = np.divide(np.nansum(rx * ry, axis=0), denominator, out=np.full(x.shape[1], np.nan), where=(denominator > 0) & (count >= min_pairs))
        values.append(pd.Series(ic, index=features.columns, name=day))
    result = pd.DataFrame(values)
    result.index.name = "date"
    return result


def ic_summary(series):
    values = series.dropna().to_numpy(dtype=float)
    n = len(values)
    if n < 2:
        return {"ic_days": n, "mean_rank_ic": None, "ic_std": None, "hac_t_lag5": None}
    centered = values - values.mean()
    long_variance = np.dot(centered, centered) / n
    for lag in range(1, min(5, n - 1) + 1):
        long_variance += 2 * (1 - lag / 6) * np.dot(centered[lag:], centered[:-lag]) / n
    se = np.sqrt(max(0, long_variance) / n)
    return {"ic_days": n, "mean_rank_ic": float(values.mean()), "ic_std": float(values.std(ddof=1)),
            "hac_t_lag5": float(values.mean() / se) if se > 0 else None}


def group_diagnostics(score, label):
    result = []
    for day, series in score.groupby(level="date"):
        series = series.dropna()
        if len(series) < 5 or series.nunique() < 5:
            continue
        bins = pd.qcut(series, 5, labels=False, duplicates="drop")
        if bins.nunique() != 5:
            continue
        for group in range(5):
            members = bins.index[bins == group]
            outcomes = label.reindex(members)
            result.append({"date": day, "group": group + 1, "signals": len(members),
                           "observed_labels": int(outcomes.count()), "mean_label": outcomes.mean()})
    return pd.DataFrame(result)
