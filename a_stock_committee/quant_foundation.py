"""Offline A-share factor research on an existing Qlib provider.

Reuses the tested tutorial factor calculations and Qlib's portfolio engine.
It deliberately has no order gateway and no automatic data download.
"""

from datetime import datetime
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from quant_learning_starter.factor_lab import (
    FACTORS, LABEL, compute_factors_and_labels, evaluate_cross_sections, summarize,
)
from .quant_inventory import ROOT

DEFAULT_CONFIG = ROOT / "committee_config/quant_foundation.json"
SEGMENTS = ("development", "validation", "test")
FIELDS = ("open", "high", "low", "close", "volume", "factor")


def read_calendar(provider):
    values = pd.to_datetime(pd.read_csv(Path(provider) / "calendars/day.txt", header=None)[0])
    calendar = pd.DatetimeIndex(values, name="date")
    if calendar.empty or calendar.hasnans or not calendar.is_unique or not calendar.is_monotonic_increasing:
        raise ValueError("Provider calendar must be sorted, unique and nonempty")
    return calendar


def validate_config(config, calendar):
    if config["market"] != "csi300" or config["board_scope"] != "main_board":
        raise ValueError("This baseline supports historical CSI300 main-board stocks only")
    previous_end = None
    for name in SEGMENTS:
        start, end = map(pd.Timestamp, config["segments"][name])
        if pd.isna(start) or pd.isna(end) or start > end or (previous_end is not None and start <= previous_end):
            raise ValueError("Segments must be strictly chronological and non-overlapping")
        if start < calendar[0] or end > calendar[-1]:
            raise ValueError(f"Requested dates exceed local coverage {calendar[0].date()} .. {calendar[-1].date()}")
        if len(calendar[(calendar >= start) & (calendar <= end)]) < 10:
            raise ValueError("Each segment needs at least 10 provider sessions")
        previous_end = end
    if calendar.searchsorted(pd.Timestamp(config["segments"]["development"][0])) < 20:
        raise ValueError("At least 20 prior sessions are required for warm-up")
    if config["baseline_factor"] not in FACTORS:
        raise ValueError("Unknown baseline factor")
    if type(config["min_pairs"]) is not int or config["min_pairs"] < 5:
        raise ValueError("min_pairs must be an integer >= 5")
    p = config["portfolio"]
    if any(type(p[k]) is not int for k in ("topk", "n_drop")) or not 0 < p["n_drop"] <= p["topk"]:
        raise ValueError("Require integer 0 < n_drop <= topk")
    numeric = ("capital", "risk_degree", "open_cost", "close_cost", "min_cost", "slippage_bps", "limit_proxy", "previous_volume_fraction")
    if any(not np.isfinite(p[k]) for k in numeric):
        raise ValueError("Portfolio settings must be finite")
    if p["capital"] <= 0 or not 0 < p["risk_degree"] <= 1:
        raise ValueError("Require positive capital and 0 < risk_degree <= 1")
    if not 0 < p["limit_proxy"] <= 0.1 or not 0 < p["previous_volume_fraction"] <= 0.1:
        raise ValueError("Unsupported execution proxy")
    if any(p[k] < 0 for k in ("open_cost", "close_cost", "min_cost", "slippage_bps")) or p["slippage_bps"] >= 10000:
        raise ValueError("Costs must be nonnegative and slippage below 10000 bps")


def read_membership(provider, config):
    path = Path(provider) / "instruments" / f"{config['market']}.txt"
    frame = pd.read_csv(path, sep="\t", header=None, names=["symbol", "start", "end"])
    frame["start"] = pd.to_datetime(frame["start"], errors="raise")
    frame["end"] = pd.to_datetime(frame["end"], errors="raise")
    if frame.isna().any().any() or (frame.start > frame.end).any():
        raise ValueError("Invalid instrument membership interval")
    main_board = frame.symbol.str.match(r"^(SH60|SZ00)\d{4}$")
    start = pd.Timestamp(config["segments"]["development"][0])
    end = pd.Timestamp(config["segments"]["test"][1])
    return frame.loc[main_board & (frame.start <= end) & (frame.end >= start)].copy()


def membership_mask(index, membership):
    """Membership is selected at the signal date, never from today's constituents."""
    dates = index.get_level_values("date")
    symbols = index.get_level_values("symbol")
    result = np.zeros(len(index), dtype=bool)
    for row in membership.itertuples(index=False):
        result |= (symbols == row.symbol) & (dates >= row.start) & (dates <= row.end)
    return pd.Series(result, index=index)


def inspect_panel(panel):
    if panel.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate date/symbol observations")
    if panel.empty:
        raise ValueError("Empty panel")
    numeric = panel[list(FIELDS)].astype(float)
    prices = numeric[["open", "high", "low", "close"]]
    valid_price = np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
    # Provider float32 adjusted prices can differ by a few units in the last place.
    tolerance = prices.max(axis=1) * 2e-6
    consistent = (prices.high + tolerance >= prices[["open", "close", "low"]].max(axis=1)) & (
        prices.low - tolerance <= prices[["open", "close", "high"]].min(axis=1))
    volume_ok = np.isfinite(numeric.volume) & numeric.volume.gt(0)
    observed = numeric.close.notna()
    bad_factor = observed & (~np.isfinite(numeric.factor) | numeric.factor.le(0))
    usable = valid_price & consistent & volume_ok
    quality = {
        "rows": len(panel), "symbols": panel.symbol.nunique(), "sessions": panel.date.nunique(),
        "missing_close_rows": int((~observed).sum()),
        "invalid_price_rows_with_close": int((observed & ~valid_price).sum()),
        "inconsistent_ohlc_rows": int((valid_price & ~consistent).sum()),
        "nonpositive_or_missing_volume_rows": int((~volume_ok).sum()),
        "invalid_volume_rows_with_close": int((observed & ~volume_ok).sum()),
        "invalid_factor_rows_with_close": int(bad_factor.sum()),
        "usable_rows": int(usable.sum()),
    }
    return usable, quality


def factor_frame(panel, calendar, membership):
    usable, quality = inspect_panel(panel)
    clean = panel.copy()
    clean.loc[~usable, ["open", "close", "volume"]] = np.nan
    result = compute_factors_and_labels(clean, calendar)
    index = pd.MultiIndex.from_frame(result[["date", "symbol"]])
    result["in_universe"] = membership_mask(index, membership).to_numpy()
    result["signal_eligible"] = result["in_universe"] & result[list(FACTORS)].notna().all(axis=1)
    # Do not require a future label to admit a signal. Missing exits remain missing.
    return result, quality


def split_diagnostics(frame, config):
    summaries, daily_frames, group_frames, counts = [], [], [], []
    for segment in SEGMENTS:
        start, end = map(pd.Timestamp, config["segments"][segment])
        selected = frame.loc[frame.in_universe & frame.date.between(start, end)].copy()
        cross_boundary = selected.exit_date.isna() | selected.exit_date.gt(end)
        selected.loc[cross_boundary, LABEL] = np.nan
        daily, groups = evaluate_cross_sections(selected, min_pairs=config["min_pairs"])
        if daily.empty:
            raise ValueError(f"No factor observations in {segment}")
        summary = summarize(daily, groups)
        for data in (daily, groups, summary):
            data.insert(0, "segment", segment)
        summaries.append(summary)
        daily_frames.append(daily)
        group_frames.append(groups)
        counts.append({"segment": segment, "rows": len(selected),
                       "purged_boundary_rows": int(cross_boundary.sum()),
                       "observed_labels": int(selected[LABEL].notna().sum())})
    return pd.concat(summaries), pd.concat(daily_frames), pd.concat(group_frames), counts


def build_signal(frame, factor):
    eligible = frame.loc[frame.signal_eligible, ["date", "symbol", factor]]
    result = eligible.rename(columns={"date": "datetime", "symbol": "instrument", factor: "score"})
    return result.set_index(["datetime", "instrument"]).sort_index()["score"]


def execution_settings(portfolio, codes):
    slip = portfolio["slippage_bps"] / 10000
    threshold = portfolio["limit_proxy"]
    change = "($open/Ref($close, 1)-1)"
    # Limits use the opening quote, not that day's future closing return.
    return {
        "codes": list(codes), "trade_unit": 100,
        "deal_price": (f"$open*{1 + slip}", f"$open*{1 - slip}"),
        "limit_threshold": (f"{change}>={threshold}", f"{change}<=-{threshold}"),
        "volume_threshold": ("cum", f"Ref($volume, 1)*{portfolio['previous_volume_fraction']}"),
        "open_cost": portfolio["open_cost"], "close_cost": portfolio["close_cost"],
        "min_cost": portfolio["min_cost"],
    }


def portfolio_metrics(report):
    required = report[["return", "cost", "bench", "cash", "account", "turnover"]]
    if required.empty or not np.isfinite(required.to_numpy()).all():
        raise ValueError("Backtest produced empty or non-finite portfolio metrics")
    if (report.cash < -0.01).any() or (report.account <= 0).any():
        raise ValueError("Backtest violates cash/account constraints")
    net = report["return"] - report["cost"]
    wealth = (1 + net).cumprod()
    benchmark = (1 + report["bench"]).cumprod()
    peak = wealth.cummax().clip(lower=1)
    return {
        "sessions": len(report), "net_total_return": float(wealth.iloc[-1] - 1),
        "benchmark_total_return": float(benchmark.iloc[-1] - 1),
        "relative_wealth_return": float(wealth.iloc[-1] / benchmark.iloc[-1] - 1),
        "max_drawdown": float((wealth / peak - 1).min()),
        "mean_daily_turnover": float(report.turnover.mean()), "minimum_cash": float(report.cash.min()),
    }


def load_local_panel(provider, config, calendar, membership):
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=str(provider), region="cn", expression_cache=None, dataset_cache=None,
              joblib_backend="threading", kernels=2)
    begin = calendar.searchsorted(pd.Timestamp(config["segments"]["development"][0])) - 20
    end = pd.Timestamp(config["segments"]["test"][1])
    sessions = calendar[(calendar >= calendar[begin]) & (calendar <= end)]
    codes = sorted(membership.symbol.unique())
    if not codes:
        raise ValueError("No main-board members in the requested interval")
    raw = D.features(codes, [f"${f}" for f in FIELDS], start_time=sessions[0], end_time=end)
    raw = raw.rename(columns={f"${f}": f for f in FIELDS}).reset_index()
    raw = raw.rename(columns={"datetime": "date", "instrument": "symbol"})
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Provider returned duplicate observations")
    # Preserve missing instruments/sessions; no forward-filled prices or compressed shifts.
    full = pd.MultiIndex.from_product([sessions, codes], names=["date", "symbol"])
    panel = raw.set_index(["date", "symbol"]).reindex(full).reset_index()
    panel["source"] = "local_qlib_historical"
    return panel, sessions


def backtest_blockers(quality):
    keys = ("invalid_factor_rows_with_close", "invalid_price_rows_with_close",
            "inconsistent_ohlc_rows", "invalid_volume_rows_with_close")
    reasons = [f"{key}={quality[key]}" for key in keys if quality.get(key, 0)]
    if quality.get("missing_feature_symbols"):
        reasons.append("Missing constituent price files: " + ", ".join(quality["missing_feature_symbols"]))
    return reasons


def run_portfolio(frame, config, quality, out, allow_partial_data=False):
    blockers = backtest_blockers(quality)
    if blockers and not allow_partial_data:
        raise ValueError("Backtest blocked: " + "; ".join(blockers))
    from qlib.contrib.evaluate import backtest_daily
    from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
    from qlib.data import D
    excluded_symbols = set(quality.get("quality_excluded_symbols") or [])
    excluded_symbols.update(quality.get("missing_feature_symbols") or [])
    signal_frame = frame.loc[~frame.symbol.isin(excluded_symbols)] if allow_partial_data else frame
    signal = build_signal(signal_frame, config["baseline_factor"])
    codes = sorted(signal.index.get_level_values("instrument").unique())
    if len(codes) < config["portfolio"]["topk"]:
        raise ValueError("Not enough signal instruments for configured topk")
    start, end = config["segments"]["test"]
    dates = D.calendar(start_time=start, end_time=end)
    benchmark = D.features(["SH000300"], ["$close"], start_time=start, end_time=end)
    if len(benchmark) != len(dates) or not np.isfinite(benchmark["$close"]).all() or benchmark["$close"].le(0).any():
        raise ValueError("Incomplete CSI300 benchmark; refusing to substitute a cash benchmark")
    p = config["portfolio"]
    strategy = TopkDropoutStrategy(signal=signal, topk=p["topk"], n_drop=p["n_drop"],
                                   risk_degree=p["risk_degree"], hold_thresh=1,
                                   only_tradable=False, forbid_all_trade_at_limit=True)
    report, positions = backtest_daily(start_time=start, end_time=end, strategy=strategy,
                                       account=p["capital"], benchmark="SH000300",
                                       exchange_kwargs=execution_settings(p, codes))
    metrics = portfolio_metrics(report)
    if len(report) != len(dates) or report.turnover.sum() <= 0:
        raise ValueError("Backtest has no trades or incomplete dates")
    report.to_csv(out / "portfolio_daily.csv", index_label="date")
    holdings = []
    for date, position in positions.items():
        for symbol in position.get_stock_list():
            amount = position.get_stock_amount(symbol)
            if amount < 0:
                raise ValueError("Unexpected short position")
            holdings.append({"date": date, "symbol": symbol, "adjusted_shares": amount,
                             "marked_price": position.get_stock_price(symbol)})
    pd.DataFrame(holdings).to_csv(out / "holdings.csv", index=False)
    signal.rename("score").to_frame().to_parquet(out / "scores.parquet")
    metrics["status"] = "historical_execution_proxy_partial_data" if blockers else "historical_execution_proxy_only"
    metrics["data_quality_gate"] = "partial_symbol_exclusion" if blockers else "clean"
    metrics["data_quality_blockers"] = blockers
    metrics["excluded_symbols"] = sorted(excluded_symbols)
    return metrics


LIMITATIONS = [
    "Historical local snapshot, not current prices; no live execution or investment recommendation.",
    "Provider membership intervals are used as supplied; announcement-time availability is not verified.",
    "Missing constituents remain missing and are counted, not silently replaced by today's survivors.",
    "No historical ST, delisting status, IPO exceptions or exact exchange limit prices are available.",
    "Main board only. The 9.5% opening-change block is a proxy, not board/date-specific trading rules.",
    "Qlib adjusted bars/factors are used by its native engine; source corporate-action correctness is unverified.",
    "Daily opening fills, fixed slippage and prior-day volume capacity omit auction queues and intraday microstructure.",
    "hold_thresh=1 with one daily decision enforces a minimum holding step, not a broker T+1 settlement ledger.",
    "Five-session IC/group returns overlap; they are diagnostics, not compounded portfolio returns or significance tests.",
    "The fixed momentum baseline was not fitted or selected using this run's test results; this is not a trained model.",
    "Removed constituents can persist until the TopkDropout turnover and tradability conditions allow selling.",
    "Fee settings are historical research assumptions, not a current broker tariff or tax schedule.",
]


def run_research(config_path=DEFAULT_CONFIG, out=None, backtest=False, allow_partial_backtest=False):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = (ROOT / config["provider_uri"]).resolve()
    calendar = read_calendar(provider)
    validate_config(config, calendar)
    membership = read_membership(provider, config)
    out = Path(out) if out else ROOT / "outputs/quant_foundation" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out.mkdir(parents=True, exist_ok=False)
    record = {
        "started_at": datetime.now().astimezone().isoformat(), "status": "running",
        "command": sys.argv, "python": sys.executable, "config": config,
        "source": "real_local_historical_data", "live_ready": False, "strategy_validated": False,
        "provider_coverage": [str(calendar[0].date()), str(calendar[-1].date())],
        "limitations": LIMITATIONS,
        "versions": {name: metadata.version(name) for name in ("pandas", "numpy", "scipy", "pyqlib")},
        "input_hashes": {str(p): sha256(p.read_bytes()).hexdigest() for p in (
            config_path, Path(__file__), ROOT / "quant_learning_starter/factor_lab.py",
            provider / "calendars/day.txt", provider / "instruments/csi300.txt")},
    }
    def save_record():
        (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    save_record()
    try:
        panel, sessions = load_local_panel(provider, config, calendar, membership)
        frame, quality = factor_frame(panel, sessions, membership)
        active = frame.in_universe
        row_usable, _ = inspect_panel(panel)
        quality["active_member_rows"] = int(active.sum())
        quality["active_member_usable_rows"] = int((active.to_numpy() & row_usable.to_numpy()).sum())
        quality["quality_excluded_symbols"] = sorted(
            panel.loc[active.to_numpy() & ~row_usable, "symbol"].dropna().unique().tolist()
        )
        quality["missing_feature_symbols"] = [s for s in sorted(membership.symbol.unique())
            if not (provider / "features" / s.lower() / "close.day.bin").exists()]
        (out / "data_quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
        panel.loc[~row_usable & panel.close.notna()].to_csv(out / "invalid_bars.csv", index=False)
        coverage = pd.DataFrame({"date": frame.date, "active_members": active.astype(int),
                                 "usable_members": (active.to_numpy() & row_usable.to_numpy()).astype(int)})
        coverage.groupby("date").sum().to_csv(out / "coverage_by_date.csv")
        summary, daily, groups, counts = split_diagnostics(frame, config)
        membership.to_csv(out / "membership.csv", index=False)
        panel.to_parquet(out / "panel.parquet", index=False)
        frame.to_parquet(out / "factors_and_labels.parquet", index=False)
        summary.to_csv(out / "factor_summary.csv", index=False)
        daily.to_csv(out / "daily_ic.csv", index=False)
        groups.to_csv(out / "factor_groups.csv", index=False)
        record.update(data_quality=quality, segments=counts, portfolio={"status": "not_requested"})
        if backtest:
            blockers = backtest_blockers(quality)
            if blockers and not allow_partial_backtest:
                record["portfolio"] = {"status": "blocked_data_quality", "reasons": blockers}
            else:
                record["portfolio"] = run_portfolio(
                    frame, config, quality, out,
                    allow_partial_data=allow_partial_backtest,
                )
        status = (
            "partial_backtest_blocked"
            if record["portfolio"]["status"] == "blocked_data_quality"
            else "completed_partial_data"
            if record["portfolio"].get("status") == "historical_execution_proxy_partial_data"
            else "completed"
        )
        record.update(status=status, finished_at=datetime.now().astimezone().isoformat())
        save_record()
        lines = ["# A-share quantitative foundation run", "", f"Run: `{out.name}`", "",
                 f"Run status: {status}. Factor diagnostics completed. Strategy profitability: not established. Live readiness: false.", "",
                 "Real local historical data; not a synthetic demo. No model fitting or parameter search.", "",
                 f"Provider coverage: {' to '.join(record['provider_coverage'])}",
                 f"Panel: {quality['symbols']} symbols, {quality['sessions']} sessions, {quality['rows']} rows.",
                 f"Usable active-member rows: {quality['active_member_usable_rows']} / {quality['active_member_rows']}.",
                 "", "## Factor diagnostics", "", "```text", summary.to_string(index=False), "```", "",
                 "Signal after close t; label: open(t+6)/open(t+1)-1. Boundary-crossing labels purged per segment.", "",
                 "## Native Qlib portfolio", "", "```json", json.dumps(record["portfolio"], indent=2), "```", "",
                 "When admitted, the baseline uses the previous session's score and next open, with cash, lots, costs and execution proxies.", "",
                 "## Limitations", ""]
        lines += [f"- {item}" for item in LIMITATIONS]
        if record["portfolio"].get("status") == "historical_execution_proxy_partial_data":
            lines += [
                "",
                "## Quality-gated partial replay boundary",
                "",
                f"Excluded symbols: {len(record['portfolio'].get('excluded_symbols') or [])}; "
                f"retained blockers: {', '.join(record['portfolio'].get('data_quality_blockers') or [])}.",
                "This replay excludes symbols with active-period quality failures and is diagnostic only; "
                "it is not a complete-universe result, strategy validation, paper promotion or live readiness.",
            ]
        lines += ["", "## Next work", "",
                  "Obtain validated current unadjusted/adjusted prices, exchange calendar, historical security status,",
                  "point-in-time constituents and corporate actions before promoting any strategy to paper execution.", "",
                  "Engine reference: https://qlib.readthedocs.io/en/latest/component/strategy.html"]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        save_record()
        raise
    return out, record
