"""Long-only monthly equal-risk allocation, not a second account or backtester."""
from dataclasses import replace

import cvxpy as cp
import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from .fund_trend import total_return_growth, START
from .monthly_timing import month_ends

SOURCE = "http://www.thierry-roncalli.com/download/erc.pdf"
WINDOW = 252
VARIANTS = ("monthly_fixed_sleeves", "monthly_equal_risk")
CONTROL_VARIANTS = VARIANTS + ("static_2023_equal_risk", "sovereign_bond_sleeve")
BOND = "511010.SSE"


def equal_risk_weights(covariance):
    covariance = np.asarray(covariance, dtype=float)
    if (covariance.ndim != 2 or covariance.shape[0] != covariance.shape[1]
            or covariance.shape[0] < 2 or not np.isfinite(covariance).all()
            or not np.allclose(covariance, covariance.T, atol=1e-15, rtol=1e-12)
            or np.linalg.eigvalsh(covariance).min() <= 0):
        raise ValueError("ERC requires a finite symmetric positive-definite covariance")
    normalized = covariance / np.trace(covariance) * len(covariance)
    y = cp.Variable(len(covariance), pos=True)
    # Equation7: variance minimization under a log-diversification constraint;
    # c=0 fixes scale only, and normalization gives the long-only ERC weights.
    problem = cp.Problem(cp.Minimize(cp.quad_form(y, normalized)), [cp.sum(cp.log(y)) >= 0])
    problem.solve(solver="CLARABEL", tol_gap_abs=1e-10, tol_gap_rel=1e-10, tol_feas=1e-10)
    if problem.status != cp.OPTIMAL or y.value is None or not np.isfinite(y.value).all() or (y.value <= 0).any():
        raise ValueError("The declared ERC solver did not produce a positive optimum")
    weights = y.value / y.value.sum()
    shares = weights * (covariance @ weights) / (weights @ covariance @ weights)
    if abs(shares - 1 / len(weights)).max() > 1e-5:
        raise ValueError("ERC numerical solution does not equalize estimated risk")
    return weights, shares


def risk_table(base, symbols, end):
    returns = total_return_growth(base, symbols, end).iloc[1:] - 1
    rows = []
    for day in month_ends(base.calendar, end):
        if day < START:
            continue
        window = returns.loc[:day].tail(WINDOW)
        if len(window) != WINDOW:
            raise ValueError("One full causal covariance window is required before a fund decision")
        covariance = window.cov(ddof=1).to_numpy()
        weights, shares = equal_risk_weights(covariance)
        for symbol, weight, share in zip(symbols, weights, shares):
            rows.append({"date": day, "symbol": symbol, "weight_fraction": float(weight),
                         "estimated_risk_share": float(share), "window_first": window.index[0],
                         "window_last": window.index[-1], "observations": len(window)})
    return pd.DataFrame(rows)


def static_training_allocation(base, symbols):
    """Freeze a full observed2023 covariance, without borrowing later returns."""
    growth = total_return_growth(base, symbols, "2023-12-29")
    if growth.index[0] != "2023-01-03" or growth.index[-1] != "2023-12-29":
        raise ValueError("The declared complete2023 warmup is required for the static control")
    returns = growth.iloc[1:] - 1
    weights, shares = equal_risk_weights(returns.cov(ddof=1).to_numpy())
    return {"calibration_period": [growth.index[0], growth.index[-1]],
            "window_first": returns.index[0], "window_last": returns.index[-1],
            "observations": len(returns), "covariance": "sample_ddof1",
            "weight_fractions": dict(zip(symbols, map(float, weights))),
            "estimated_risk_shares": dict(zip(symbols, map(float, shares))),
            "frozen": True, "uses_evaluation_returns": False}


class FundRiskFeed:
    def __init__(self, base, table, variant, symbols, end, *, static_allocation=None):
        if variant not in CONTROL_VARIANTS or end > base.manifest["end"]:
            raise ValueError("Unknown declared fund allocation or cutoff")
        self.static_weights = None
        if variant == "static_2023_equal_risk":
            fractions = (static_allocation or {}).get("weight_fractions", {})
            if (set(fractions) != set(symbols) or not all(np.isfinite(v) and v > 0 for v in fractions.values())
                    or abs(sum(fractions.values()) - 1) > 1e-12
                    or static_allocation.get("window_last", "9999-12-31") >= START):
                raise ValueError("Static allocation requires complete positive weights calibrated before replay")
            self.static_weights = dict(fractions)
        if variant == "sovereign_bond_sleeve" and BOND not in symbols:
            raise ValueError("The declared sovereign-bond control is missing its instrument")
        self.base, self.variant, self.symbols, self.end = base, variant, tuple(symbols), end
        self.ends = set(month_ends(base.calendar, end))
        self.weights = {day: dict(zip(rows.symbol, rows.weight_fraction)) for day, rows in table.groupby("date")}
        self.audit = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or day > self.end:
            raise ValueError("Fixed fund risk allocation is historical-replay-only within its cutoff")
        if (config.signal != "equal_weight" or config.rebalance_sessions != 1
                or config.holdings != len(self.symbols) or config.retain_rank != len(self.symbols)
                or config.gross_limit != .8 or config.single_limit != .8):
            raise ValueError("Fund risk allocation requires its fixed declared configuration")
        if day not in self.ends:
            return SignalUnavailable("not_month_end")
        return SignalFrame.from_legacy([{"symbol": s, "score": 1.} for s in self.symbols],
                                      day, "research:" + self.variant, context)

    def allocate(self, intent):
        day = intent.decided_at[:10]
        if self.variant == VARIANTS[0]:
            weights = dict(intent.weights)
        elif self.variant == "sovereign_bond_sleeve":
            weights = {s: .8 if s == BOND else 0. for s in self.symbols}
        elif self.variant == "static_2023_equal_risk":
            weights = {s: float(.8 * self.static_weights[s]) for s in self.symbols}
        else:
            fractions = self.weights.get(day)
            if fractions is None or set(fractions) != set(self.symbols):
                raise ValueError("Missing declared covariance allocation at month-end")
            weights = {s: float(.8 * fractions[s]) for s in self.symbols}
        self.audit.extend({"date": day, "symbol": s, "variant": self.variant, "gate": "monthly_allocation",
                           "desired_weight": weight} for s, weight in weights.items())
        return replace(intent, weights=tuple(weights.items()))
