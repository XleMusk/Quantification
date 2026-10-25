# Common-window strategy comparison (2026-10-26)

Common saved-account window: `2024-01-31..2026-10-13` (648 sessions).

This is a derived comparison over the same frozen StockDB snapshot and shared Engine/Ledger artifacts; it does not rerun strategies or access broker/THS channels.

| Strategy | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe | Order fees | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 低波/半动量 (`low_vol_half_momentum_20`) | 648 | 390 | 656 | 14.7803% | -16.5805% | 0.5187 | 2448.16 | CONTINUE_RESEARCH_ONLY |
| 成熟因子 Ridge (`mature_factor_balanced_ridge`) | 648 | 281 | 562 | 36.9764% | -18.1384% | 0.7620 | 4584.21 | CONTINUE_RESEARCH_ONLY |
| 成熟因子 Ridge（60-session variant） (`mature_factor_balanced_ridge_h60`) | 648 | 279 | 556 | 32.1767% | -19.1039% | 0.7401 | 4129.55 | CONTINUE_RESEARCH_ONLY |

## Boundaries

- The common window is derived from saved completed account artifacts; no strategy was rerun.
- The overlap comparison does not establish PIT completeness, forward validation, or adoption permission.
- Simulated fills are not broker or Tonghuashun observations; execution_admitted remains false.
