# Common-window extra cost pressure

Window: `2024-01-31..2026-10-13` (648 sessions).

Extra costs are applied to saved daily turnover using the same account rows; no strategy was rerun.

| Strategy | Base return | +50bp return | +100bp return | +50bp extra cost | +100bp extra cost | Decision unchanged |
|---|---:|---:|---:|---:|---:|---|
| 低波/半动量 (`low_vol_half_momentum_20`) | 14.7803% | 7.6556% | 0.5309% | 9974.53 | 19949.07 | CONTINUE_RESEARCH_ONLY |
| 成熟因子 Ridge (`mature_factor_balanced_ridge`) | 36.9764% | 7.5941% | -21.7881% | 41135.10 | 82270.20 | CONTINUE_RESEARCH_ONLY |
| 成熟因子 Ridge（60-session variant） (`mature_factor_balanced_ridge_h60`) | 32.1767% | 5.6881% | -20.8005% | 37084.00 | 74168.01 | CONTINUE_RESEARCH_ONLY |

The pressure sensitivity does not promote or stop a candidate; it is an economic robustness input for the next research decision.
