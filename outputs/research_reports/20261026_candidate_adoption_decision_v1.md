# Candidate adoption gate (research only)

Common window: `2024-01-31..2026-10-13` (648 sessions).

This gate joins the common-window comparison, cost pressure and latest research selection. It never promotes a candidate.

| Strategy | Decision | Reason | Max cost pressure | Strategy validated |
|---|---|---|---:|---|
| 低波/半动量 (`low_vol_half_momentum_20`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |
| 成熟因子 Ridge (`mature_factor_balanced_ridge`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |
| 成熟因子 Ridge（60-session variant） (`mature_factor_balanced_ridge_h60`) | CONTINUE_RESEARCH_ONLY | common-window evidence is research-only and not strategy validated | 100 bps | no |

All rows keep `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.
