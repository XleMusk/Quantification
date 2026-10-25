# Candidate adoption gate (research only)

Common window: `2024-01-31..2026-10-13` (648 sessions).

This gate joins the common-window comparison, cost pressure and latest research selection. It never promotes a candidate.

| Strategy | Decision | Reason | Max cost pressure | Strategy validated |
|---|---|---|---:|---|
| 低波/半动量 (`low_vol_half_momentum_20`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |
| 成熟因子 Ridge (`mature_factor_balanced_ridge`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |
| 成熟因子 Ridge（60-session variant） (`mature_factor_balanced_ridge_h60`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |
| stockdb_low_volatility_252_20 (`fixed_low_volatility_252_20`) | STOP_RESEARCH_ONLY | Preserve the negative evidence and do not repeat the same lineage without changed data or code. | n/a | no |
| stockdb_momentum_12_1_20 (`momentum_12_1_20`) | BLOCKED_COVERAGE | Obtain the missing common forward coverage before ranking or replay. | n/a | no |
| alpha158_lgbm_candidate_panel_v1 (`alpha158_lgbm_cap500`) | STOP_RESEARCH_ONLY | Preserve the negative evidence and do not repeat the same lineage without changed data or code. | n/a | no |
| stockdb_value_size_activity_2023 (`None`) | UNASSESSED_SEPARATE_GATE | Keep the candidate in its own data and timing gate; no selection or promotion. | n/a | no |
| shortline_native_lr_lgbm (`None`) | UNASSESSED_SEPARATE_GATE | Keep the candidate in its own data and timing gate; no selection or promotion. | n/a | no |
| public_seat_trend_hypotheses (`None`) | UNASSESSED_SEPARATE_GATE | Keep the candidate in its own data and timing gate; no selection or promotion. | n/a | no |
| leader_eight_type_replay_engine (`None`) | UNASSESSED_SEPARATE_GATE | Keep the candidate in its own data and timing gate; no selection or promotion. | n/a | no |

All rows keep `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.
