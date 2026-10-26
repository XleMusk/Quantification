# Shortline native candidate handoff

Source plan: `D:\QuantificationData\trading\research\shortline_native_preopen\20261016_source1015_ipoqualified_v1\plan.json`; source day `2026-10-15`; execution day `2026-10-16`.

The adapter copied only the explicitly selected rows from the published qualified preopen plan into a new handoff. The raw probability payload and source plan were not modified.

| Model | Selected rows | Candidate file | Score gate |
|---|---:|---|---|
| lr | 3 | `lr_candidates.csv` | `VERIFIED_SHORTLINE_SCORE_GATE_READY` (3 marked) |
| lgbm | 3 | `lgbm_candidates.csv` | `VERIFIED_SHORTLINE_SCORE_GATE_READY` (3 marked) |

The handoff is research-only and candidate-panel-only. It does not generate a target, order, fill, account or THS payload; PIT membership, company-action safety and full-window returns remain unproven.
