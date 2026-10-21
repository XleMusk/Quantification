from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class StrategyConfig:
    signal: str = "risk_adjusted_momentum"
    lookback: int = 126
    skip: int = 21
    volatility_window: int = 60
    rebalance_sessions: int = 5
    holdings: int = 5
    retain_rank: int = 8
    gross_limit: float = .8
    single_limit: float = .2
    initial_cash: float = 100000
    min_amount: float = 20000000
    min_listing_days: int = 365
    commission: float = .0003
    minimum_commission: float = 5
    sell_tax: float = .0005
    transfer_fee: float = .00001
    slippage_bps: float = 10
    participation: float = .01
    dividend_tax_reserve: float = .2

    def validate(self):
        if self.signal not in {"risk_adjusted_momentum", "momentum", "low_vol_momentum",
                               "liquid_momentum", "equal_weight"}:
            raise ValueError("Unknown signal")
        for key in ("lookback", "skip", "volatility_window", "rebalance_sessions", "holdings", "retain_rank", "min_listing_days"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"Invalid integer setting: {key}")
        if not 21 <= self.lookback <= 252 or not self.skip < self.lookback or not 10 <= self.volatility_window <= 126:
            raise ValueError("Invalid research windows")
        if not 1 <= self.holdings <= 22 or not self.holdings <= self.retain_rank <= 100:
            raise ValueError("Invalid holdings/rank buffer")
        for key, value in asdict(self).items():
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Non-finite/negative setting: {key}")
        if not 0 < self.single_limit <= self.gross_limit <= 1 or not 1000 <= self.initial_cash <= 10000000:
            raise ValueError("Invalid capital bounds")
        if not 0 < self.participation <= .1 or not 0 <= self.slippage_bps <= 100 or not 0 <= self.dividend_tax_reserve <= 1:
            raise ValueError("Invalid execution assumptions")
        if max(self.commission, self.sell_tax, self.transfer_fee) > .01 or self.minimum_commission > 100:
            raise ValueError("Invalid fee assumptions")
        return self

    def to_dict(self):
        return asdict(self)
