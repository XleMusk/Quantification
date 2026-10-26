# Current-window fixed versus half-momentum comparison

Common saved-account window: `2026-08-31..2026-10-26` (35 sessions).

This report reads the explicit 2026-10-26 StockDB replay artifacts on a shared Engine/Ledger path. It is a research-only terminal-window diagnostic; no broker or Tonghuashun channel was accessed.

| Strategy | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe | Fees (RMB) | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 固定低波动 (`low_volatility_252_20`) | 35 | 25 | 44 | 0.8293% | -2.0255% | 0.8366 | 129.93 | CONTINUE_RESEARCH_ONLY |
| 低波/半动量 (`low_vol_half_momentum_20`) | 35 | 40 | 72 | -9.4132% | -9.6664% | -5.8082 | 256.10 | STOP_RESEARCH_ONLY |

`execution_admitted=false`; `strategy_validated=false`; `promotion_allowed=false`.

## Boundaries

- The comparison reads completed research-only account artifacts and does not rerun either strategy.
- The terminal overlap is a short current-window diagnostic; it does not replace long-window or prospective validation.
- PIT completeness, broker/THS observation, strategy validation and promotion remain unproven.
