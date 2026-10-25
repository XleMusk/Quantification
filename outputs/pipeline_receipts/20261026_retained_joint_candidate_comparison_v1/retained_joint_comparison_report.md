# Retained candidate joint comparison

Common window: `2024-01-31..2026-10-13` (648 sessions).
Joint terminal target day: `2026-10-13`; same StockDB snapshot: `D:\QuantificationData\trading\research\full_market\stockdb_2024_0101_1013_current_cutoff_v1`.

| Sleeve | Net return | Max drawdown | Sharpe | Orders | Fills | Fees CNY |
|---|---:|---:|---:|---:|---:|---:|
| `low_vol_half_momentum_20` | 14.7803% | -16.5805% | 0.5187 | 390 | 656 | 2448.16 |
| `mature_factor_balanced_ridge` | 36.9764% | -18.1384% | 0.7620 | 281 | 562 | 4584.21 |

Joint target: 22 rows, 22 paper orders, 21 routes available, modeled fees CNY 110.71.
Contributor rows: {'trend_low_vol_half_momentum_20': 20, 'stockdb_factor_monthly_ridge_v1': 2}; capacity dropped: 3.

The sleeve return spread is a common-window diagnostic. A joint return increment is deliberately not calculated because the joint artifact is a terminal target projection, not a forward account.

`execution_admitted=false`; no broker or Tonghuashun interaction occurred.
