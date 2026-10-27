# Derived fixed_daily D0/D1 v5

The registered derived StockDB fixed_daily account reached the 2026-10-26 close. The pinned snapshot covers all 20 requested symbols, and the D0/D1 receipt now exposes the resolved dates at the top level: close `2026-10-26`, execution `2026-10-27`, settlement `2026-10-28`.

The fixed low-volatility leg produced 20 target holdings and 20 TARGET_PREVIEW paper orders. The same shared projection produced 20 joint paper orders. The same-day candidate manifest had zero registered candidates, so no older candidate was reused or relabeled as current data.

The receipt preserves `execution_admitted=false`, no broker or THS interaction, no persistent ledger mutation, and derived StockDB research-only execution references. Focused D0/D1 and post-close tests: `27 passed`. Full receipt: `D:\Quantification\outputs\pipeline_receipts\20261027_post_close_derived_d0_d1_v5\contract_receipt.json`.
