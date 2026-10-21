import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from .settings import DATASET
from .contracts import Price, PriceBasis, SignalFrame
from .rules import (execution_rule, holder_distribution_action, holder_restructuring_ex_price,
                    restructuring_ex_price, stock_board)


class HistoricalFeed:
    def __init__(self, folder=DATASET):
        self.folder = Path(folder)
        self.manifest = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest["status"] != "ready":
            raise ValueError("Historical dataset is not ready")
        self.folder = Path(self.manifest.get("folder", self.folder))
        self.calendar = pd.read_csv(self.folder / "calendar.csv").date.tolist()
        self.raw = self._bars("raw.parquet")
        self.adjusted = self._bars("adjusted.parquet")
        self.instruments = pd.read_csv(self.folder / "instruments.csv", keep_default_na=False).set_index("symbol").to_dict("index")
        self.actions = pd.read_csv(self.folder / "actions.csv", keep_default_na=False).to_dict("records")
        profiles = self.manifest.get("instrument_profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("Instrument profiles must be an explicit security/profile map")
        self.instrument_profiles = dict(profiles)
        self.symbols = sorted(self.instruments)
        self.closes = self.adjusted.pivot(index="date", columns="symbol", values="close").reindex(self.calendar)
        self.amounts = self.raw.pivot(index="date", columns="symbol", values="amount").reindex(self.calendar)
        self.volumes = self.raw.pivot(index="date", columns="symbol", values="volume").reindex(self.calendar)
        self.rows = {day: frame.set_index("symbol").to_dict("index") for day, frame in self.raw.groupby("date")}
        self._features = {}
        self.validate()

    def _bars(self, name):
        frame = pd.read_parquet(self.folder / name)
        frame["date"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
        for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus", "isST"):
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        if frame.duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate market observations")
        return frame.sort_values(["date", "symbol"])

    def validate(self):
        if self.calendar != sorted(set(self.calendar)) or not self.calendar:
            raise ValueError("Invalid exchange calendar")
        if not self.raw.date.isin(self.calendar).all():
            raise ValueError("Bars outside exchange calendar")
        if not self.raw.adjustflag.astype(str).eq("3").all():
            raise ValueError("Execution requires unadjusted prices")
        if not self.adjusted.adjustflag.astype(str).eq("2").all():
            raise ValueError("Research requires the declared forward-adjusted prices")
        keys = ["date", "symbol"]
        if not self.raw[keys].reset_index(drop=True).equals(self.adjusted[keys].reset_index(drop=True)):
            raise ValueError("Raw/adjusted observations are not aligned")
        if not set(self.raw.symbol) <= set(self.instruments):
            raise ValueError("Instrument metadata missing for market observations")
        profiles = getattr(self, "instrument_profiles", {})
        if not isinstance(profiles, dict) or not set(profiles) <= set(self.instruments):
            raise ValueError("Instrument profile has no matching security metadata")
        for symbol, info in self.instruments.items():
            if symbol in profiles:
                rule = execution_rule(symbol, self.manifest["start"], profiles)
                execution_rule(symbol, self.manifest["end"], profiles)
                if (not rule.is_fund or info.get("instrument_type") != rule.fund_type
                        or info.get("board") not in {None, ""}):
                    raise ValueError(f"Fund profile/type metadata conflict: {symbol}")
                continue
            board = stock_board(symbol)
            if info.get("board") and info["board"] != board:
                raise ValueError(f"Board/code metadata conflict: {symbol}")
        active = self.raw.tradestatus.eq(1)
        bars = self.raw.loc[active]
        prices = bars[["open", "high", "low", "close", "preclose"]]
        if not np.isfinite(prices).all().all() or prices.le(0).any().any():
            raise ValueError("Invalid executable prices")
        if (bars.high + 1e-7 < bars[["open", "low", "close"]].max(axis=1)).any() or (bars.low - 1e-7 > bars[["open", "high", "close"]].min(axis=1)).any():
            raise ValueError("Inconsistent OHLC")
        if not self.raw.isST.isin([0, 1]).all() or not self.raw.tradestatus.isin([0, 1]).all():
            raise ValueError("Historical status is missing")
        for symbol in profiles:
            fund_rows = self.raw.loc[self.raw.symbol.eq(symbol)]
            if not fund_rows.isST.eq(0).all():
                raise ValueError("Fund risk status is not normalized; do not reinterpret provider isST")
            for row in fund_rows.loc[fund_rows.tradestatus.eq(1)].to_dict("records"):
                for field in ("open", "high", "low", "close", "preclose"):
                    Price.from_rmb(row[field], PriceBasis.RAW, "mill")
        action_keys = {(a["symbol"], a["dividOperateDate"]) for a in self.actions}
        if len(action_keys) != len(self.actions):
            raise ValueError("Duplicate corporate-action identity; reconcile before crediting cash/shares")
        fund_actions = {}
        for action in self.actions:
            if action["symbol"] not in profiles:
                continue
            amounts = [Decimal(str(action.get(field) or "0")) for field in (
                "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")]
            chronology = [date.fromisoformat(action[field]) for field in (
                "dividRegistDate", "dividOperateDate", "dividPayDate")]
            if (not all(value.is_finite() for value in amounts) or amounts[0] <= 0
                    or any(amounts[1:]) or chronology != sorted(chronology) or chronology[0] >= chronology[1]):
                raise ValueError("Fund profile supports only reconciled cash-only distributions")
            source = (self.folder / action.get("source_file", "")).resolve()
            source.relative_to(self.folder.resolve())
            if (not str(action.get("source_url", "")).startswith("https://") or not source.is_file()
                    or not source.read_bytes().startswith(b"%PDF-")
                    or sha256(source.read_bytes()).hexdigest() != action.get("source_sha256")):
                raise ValueError("Fund cash event needs its pinned primary-source PDF")
            fund_actions[(action["symbol"], action["dividOperateDate"])] = amounts[0]
        adjustments = self.manifest.get("historical_price_adjustments", [])
        adjustment_keys = {(a["symbol"], a["date"]) for a in adjustments}
        distribution_keys = {(a["symbol"], a["date"]) for a in adjustments
                             if a["kind"] == "restructuring_holder_distribution"}
        if (len(adjustment_keys) != len(adjustments)
                or (adjustment_keys & action_keys) - distribution_keys
                or not distribution_keys <= action_keys):
            raise ValueError("Duplicate or overlapping historical price adjustments")
        for item in adjustments:
            source = (self.folder / item["source_file"]).resolve()
            source.relative_to(self.folder.resolve())
            during_replay = item["date"] >= self.manifest["start"]
            if (item["kind"] not in {"rights_issue", "restructuring_no_entitlement", "restructuring_holder_distribution"}
                    or (during_replay and item["date"] > self.manifest.get("end", ""))
                    or (during_replay and item["kind"] == "rights_issue"
                        and (item.get("ledger_policy") != "stop_unresolved_registered_holding"
                             or item["registration_date"] not in self.calendar))
                    or not date.fromisoformat(item["registration_date"]) < date.fromisoformat(item["date"])
                    or date.fromisoformat(item["source_date"]) > date.fromisoformat(item["date"])
                    or not source.is_file() or sha256(source.read_bytes()).hexdigest() != item["source_sha256"]):
                raise ValueError("Unsupported or unverified historical price adjustment")
            if item["kind"] == "restructuring_holder_distribution":
                holder_source = (self.folder / item["holder_source_file"]).resolve()
                holder_source.relative_to(self.folder.resolve())
                if (not holder_source.is_file()
                        or sha256(holder_source.read_bytes()).hexdigest() != item["holder_source_sha256"]
                        or date.fromisoformat(item["holder_source_date"]) > date.fromisoformat(item["date"])
                        or (during_replay and (item.get("ledger_policy") != "reconcile_registered_holder_distribution"
                                               or item["registration_date"] not in self.calendar))):
                    raise ValueError("Unsupported or unverified holder distribution")
                expected_action = holder_distribution_action(item)
                actual_action = next(a for a in self.actions if (a["symbol"], a["dividOperateDate"])
                                     == (item["symbol"], item["date"]))
                amounts = {"dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"}
                for field, value in expected_action.items():
                    if field in amounts:
                        equal = float(actual_action.get(field, "nan")) == float(value)
                    else:
                        equal = actual_action.get(field) == value
                    if not equal:
                        raise ValueError("Holder distribution does not match pinned reference terms")
            if item["kind"] == "rights_issue":
                ratio, price = float(item["rights_ratio"]), float(item["subscription_price"])
                if not np.isfinite([ratio, price]).all() or not 0 < ratio <= 1 or price <= 0:
                    raise ValueError("Invalid rights-issue ratio or price")
        explained_adjustments = set()
        for symbol, rows in self.raw.groupby("symbol"):
            prior = rows.close.shift()
            if symbol in profiles:
                for index in rows.index[rows.tradestatus.eq(1) & prior.gt(0)]:
                    cash = fund_actions.get((symbol, rows.at[index, "date"]))
                    if cash is not None:
                        expected = (Decimal(str(prior.at[index])) - cash).quantize(
                            Decimal("0.001"), rounding=ROUND_HALF_UP)
                        if abs(expected - Decimal(str(rows.at[index, "preclose"]))) > Decimal("0.0000001"):
                            raise ValueError(f"Fund cash reference does not match raw bars: {symbol} {rows.at[index, 'date']}")
            tolerance = .0005 if symbol in profiles else .021
            discontinuity = (rows.preclose - prior).abs().gt(tolerance) & rows.tradestatus.eq(1) & prior.gt(0)
            for day in rows.loc[discontinuity, "date"]:
                key = (symbol, day)
                if key in adjustment_keys:
                    item = next(a for a in adjustments if (a["symbol"], a["date"]) == key)
                    index = rows.index[rows.date.eq(day)][0]
                    if item["kind"] == "rights_issue":
                        ratio, price = float(item["rights_ratio"]), float(item["subscription_price"])
                        expected = (prior.at[index] + ratio * price) / (1 + ratio)
                    elif item["kind"] == "restructuring_holder_distribution":
                        expected = float(holder_restructuring_ex_price(prior.at[index], item))
                    else:
                        expected = float(restructuring_ex_price(prior.at[index], item))
                    if abs(expected - rows.at[index, "preclose"]) > .011:
                        raise ValueError(f"{item['kind']} ex-price does not match raw bars: {symbol} {day}")
                    explained_adjustments.add(key)
                elif key not in action_keys:
                    raise ValueError(f"Unexplained price adjustment: {symbol} {day}; corporate-action reconciliation required")
        if explained_adjustments != adjustment_keys:
            raise ValueError("Historical price adjustment has no matching raw-bar discontinuity")

    def features(self, config):
        key = (config.lookback, config.skip, config.volatility_window)
        if key not in self._features:
            momentum = self.closes.shift(config.skip) / self.closes.shift(config.lookback) - 1
            volatility = self.closes.pct_change(fill_method=None).rolling(config.volatility_window, min_periods=config.volatility_window).std() * np.sqrt(252)
            self._features[key] = (momentum, volatility, self.amounts.rolling(20, min_periods=20).median())
        return self._features[key]

    def scores(self, day, config):
        momentum, volatility, liquidity = self.features(config)
        rows = self.rows.get(day, {})
        candidates = []
        for symbol in self.symbols:
            row, info = rows.get(symbol), self.instruments[symbol]
            if row is None or row["tradestatus"] != 1 or row["isST"] != 0:
                continue
            if pd.isna(info.get("ipoDate")) or not info.get("ipoDate"):
                continue
            if (pd.Timestamp(day) - pd.Timestamp(info["ipoDate"])).days < config.min_listing_days:
                continue
            if info.get("outDate") and day >= info["outDate"]:
                continue
            if day <= self.manifest["selection_date"]:
                continue
            m, v, amount = momentum.at[day, symbol], volatility.at[day, symbol], liquidity.at[day, symbol]
            if not np.isfinite([m, v, amount]).all() or v <= 0 or amount < config.min_amount:
                continue
            if config.signal != "equal_weight" and m <= 0:
                continue
            candidates.append({"symbol": symbol, "score": None, "momentum": float(m),
                               "volatility": float(v), "median_amount": float(amount)})
        if config.signal == "low_vol_momentum" and candidates:
            # The median is computed from today's eligible pool only. It is a
            # fixed cross-sectional gate, not a return-selected threshold.
            volatility_cutoff = float(np.median([row["volatility"] for row in candidates]))
            candidates = [row for row in candidates if row["volatility"] <= volatility_cutoff]
        elif config.signal == "liquid_momentum" and candidates:
            # Use the same point-in-time eligible pool and a fixed median
            # activity gate; the rule is declared independently of replay
            # returns and does not select a turnover threshold from outcomes.
            activity_cutoff = float(np.median([row["median_amount"] for row in candidates]))
            candidates = [row for row in candidates if row["median_amount"] >= activity_cutoff]
        for row in candidates:
            if config.signal in {"risk_adjusted_momentum", "low_vol_momentum", "liquid_momentum"}:
                row["score"] = row["momentum"] / row["volatility"]
            elif config.signal == "momentum":
                row["score"] = row["momentum"]
            else:
                row["score"] = 1.
        return sorted(candidates, key=lambda row: (-row["score"], row["symbol"]))

    def signal_frame(self, day, config, context):
        return SignalFrame.from_legacy(self.scores(day, config), day, "rule:" + config.signal, context)

    def next_day(self, day):
        index = self.calendar.index(day)
        # Beyond the loaded calendar this is a lower bound, not a guessed session.
        # Execution only advances on exchange sessions; new data must unlock T+1.
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def freeze(feed):
    """New downloads are immutable versions; preserve the initial pre-version bundle."""
    if feed.manifest.get("folder"):
        return feed.folder
    folder = feed.folder / "versions" / "initial_20260926"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv", "selection.csv", "manifest.json"):
        destination = folder / name
        if not destination.exists():
            shutil.copy2(feed.folder / name, destination)
    (folder / "manifest.json").write_text(json.dumps({**feed.manifest, "folder": str(folder)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder
