"""Reuse full native feature partitions for the existing baseline/ledger consumer."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant_learning_starter.factor_lab import compute_factors_and_labels
from .factors import LABEL, qcode


def dated_eligibility(feed, config, dates=None):
    dates = list(feed.calendar if dates is None else dates)
    positions = {day: i for i, day in enumerate(feed.calendar)}
    if dates != sorted(set(dates)) or any(day not in positions for day in dates):
        raise ValueError("Model eligibility dates must be distinct pinned calendar sessions")
    calendar = pd.to_datetime(dates)
    ipo = pd.to_datetime([feed.instruments[s].get("ipoDate") or None for s in feed.symbols])
    retired = pd.to_datetime([feed.instruments[s].get("outDate") or None for s in feed.symbols])
    liquidity = feed.amounts.rolling(20, min_periods=20).median().to_numpy()
    eligible = np.zeros((len(dates), len(feed.symbols)), dtype=bool)
    supported = set(feed.symbols) - set(feed.unsupported_instruments)
    for i, day in enumerate(dates):
        rows = feed.rows.get(day, {})
        live = np.array([s in supported and s in rows and rows[s]["tradestatus"] == 1
                         and rows[s]["isST"] == 0 and rows[s]["volume"] > 0 for s in feed.symbols])
        eligible[i] = (live & ((calendar[i] - ipo).days >= config.min_listing_days)
                       & (retired.isna() | (calendar[i] < retired))
                       & (liquidity[positions[day]] >= config.min_amount)
                       & (day > feed.manifest["selection_date"]))
        if (i + 1) % 200 == 0:
            print(f"dated model pool: {i + 1}/{len(dates)} sessions", flush=True)
    index = pd.MultiIndex.from_product([calendar, feed.symbols], names=["date", "symbol"])
    return pd.Series(eligible.ravel(), index=index, name="eligible")


def provider_labels(provider, symbols, calendar):
    panels = []
    for symbol in symbols:
        fields = {}
        for field in ("open", "close", "volume"):
            values = np.fromfile(provider / "features" / qcode(symbol).lower() / (field + ".day.bin"), dtype="<f4")
            if len(values) != len(calendar) + 1 or values[0] != 0:
                raise ValueError("Normalized provider binary calendar/header changed")
            fields[field] = values[1:].astype(float)
        panels.append(pd.DataFrame({"date": calendar, "symbol": symbol, **fields}))
    # The existing calendar-aware label function retains missing entry/exit quotes.
    labels = compute_factors_and_labels(pd.concat(panels, ignore_index=True), calendar)
    return labels.set_index(["date", "symbol"])[[LABEL, "entry_date", "exit_date"]]


def native_binding(feed, native_view, definitions):
    native_view = Path(native_view).resolve()
    receipt = json.loads((native_view / "result.json").read_text(encoding="utf-8"))
    provider_view = Path(receipt["provider_view"]).resolve()
    provider_receipt = json.loads((provider_view / "result.json").read_text(encoding="utf-8"))
    provider = Path(provider_receipt["provider_folder"]).resolve()
    provider.relative_to(provider_view)
    pinned_calendar = (provider / "calendars/day.txt").read_text(encoding="utf-8").splitlines()
    actual_catalog = pd.read_csv(native_view / "alpha158_catalog.csv", keep_default_na=False)
    if (receipt["status"] != "full_native_features_completed"
            or receipt["model_trained"] is not False or receipt["execution_admitted"] is not False
            or provider_receipt["status"] != "research_provider_completed"
            or Path(provider_receipt["price_view"]).resolve() != feed.folder
            or pinned_calendar != feed.calendar
            or receipt["symbols"] != len(feed.symbols)
            or receipt["rows"] != len(feed.symbols) * len(feed.calendar)
            or not actual_catalog.equals(definitions)):
        raise ValueError("Full native features must match the fixed price/calendar/catalog inputs")
    all_symbols = [s for part in receipt["partitions"] for s in part["symbols"]]
    if all_symbols != feed.symbols or len(all_symbols) != len(set(all_symbols)):
        raise ValueError("Native partitions must cover every canonical symbol exactly once")
    return native_view, receipt, provider


def load_inputs(feed, config, native_view, definitions, *, segments):
    native_view, receipt, provider = native_binding(feed, native_view, definitions)
    eligible = dated_eligibility(feed, config)
    calendar = pd.to_datetime(feed.calendar)
    labels = provider_labels(provider, feed.symbols, calendar)
    frames = []
    for part in receipt["partitions"]:
        path = (native_view / part["file"]).resolve()
        path.relative_to(native_view)
        frame = pd.read_parquet(path)
        wanted = pd.MultiIndex.from_product([calendar, part["symbols"]], names=["date", "symbol"]).sort_values()
        if (not frame.index.equals(wanted) or frame.columns.tolist() != definitions.name.tolist()
                or len(frame) != part["rows"]):
            raise ValueError("Native partition code/date/feature identities changed")
        selected = eligible.reindex(frame.index, fill_value=False)
        start, end = segments["train"][0], segments["test"][1]
        days = frame.index.get_level_values("date")
        frames.append(frame.loc[selected & (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))])
        print(f"reuse native partition {part['file']}: {len(frames[-1])} dated eligible rows", flush=True)
    features = pd.concat(frames).sort_index()
    if features.empty or features.index.has_duplicates or np.isinf(features.to_numpy()).any():
        raise ValueError("Nonempty distinct finite-or-missing native features required")
    quality = {"symbols": len(feed.symbols) - len(feed.unsupported_instruments),
               "source_symbols": len(feed.symbols), "calendar_grid_rows": receipt["rows"],
               "eligible_rows": int(eligible.sum()), "model_rows": len(features),
               "universe": feed.manifest["scope"], "native_view": str(native_view),
               "native_features_recomputed": False, "unsupported_instruments": feed.unsupported_instruments,
               "label_basis": "Existing O(T+6)/O(T+1)-1 on masked normalized float32 provider quotes; promoted to float64 for calculation",
               "limitations": feed.manifest["limitations"]}
    return features, labels, quality
