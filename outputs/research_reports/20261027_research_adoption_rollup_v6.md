# Research adoption rollup (research only)

This receipt joins the candidate adoption matrix with the mature-factor horizon comparison and the shortline model gate.

| Candidate | Decision | Validated |
|---|---|---|
| 低波/半动量 (`low_vol_half_momentum_20`) | CONTINUE_RESEARCH_ONLY | no |
| 成熟因子 Ridge (`mature_factor_balanced_ridge`) | CONTINUE_RESEARCH_ONLY | no |
| 成熟因子 Ridge（60-session variant） (`mature_factor_balanced_ridge_h60`) | CONTINUE_RESEARCH_ONLY | no |
| stockdb_low_volatility_252_20 (`fixed_low_volatility_252_20`) | STOP_RESEARCH_ONLY | no |
| stockdb_momentum_12_1_20 (`momentum_12_1_20`) | BLOCKED_COVERAGE | no |
| alpha158_lgbm_candidate_panel_v1 (`alpha158_lgbm_cap500`) | STOP_RESEARCH_ONLY | no |
| stockdb_value_size_activity_2023 (`None`) | UNASSESSED_SEPARATE_GATE | no |
| shortline_native_lr_lgbm (`None`) | UNASSESSED_SEPARATE_GATE | no |
| public_seat_trend_hypotheses (`None`) | UNASSESSED_SEPARATE_GATE | no |
| leader_eight_type_replay_engine (`None`) | UNASSESSED_SEPARATE_GATE | no |

Mature factor: `NO_ADOPTION_RESEARCH_ONLY`; baseline return 36.9764%, variant return 32.1767%, delta -4.7997%.

| Shortline model | Decision | Sessions | Orders | Fills | Net return |
|---|---|---:|---:|---:|---:|
| lr | CONTINUE_RESEARCH_ONLY | 2634 | 1 | 2 | -0.6032% |
| lgbm | CONTINUE_RESEARCH_ONLY | 3863 | 3 | 6 | -1.2715% |

Retained joint target: `CONTINUE_RESEARCH_ONLY` on `2026-10-13`, 22 target rows, 22 paper orders; return status `NOT_COMPARABLE_PROJECTION_ONLY`.

Latest D0/D1 paper: close `2026-10-23`, 40/40 target symbols, fixed 20 orders, joint 22 orders; source scope `persistent_registered_account`.

Latest current-window comparison: `2026-08-31..2026-10-26` (35 sessions), snapshot `D:\QuantificationData\trading\research\full_market\stockdb_2024_0101_1013_current_cutoff_v1`.
- `low_volatility_252_20`: CONTINUE_RESEARCH_ONLY, return 0.8293%, orders 25, fills 44.
- `low_vol_half_momentum_20`: STOP_RESEARCH_ONLY, return -9.4132%, orders 40, fills 72.

All rows keep `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.
