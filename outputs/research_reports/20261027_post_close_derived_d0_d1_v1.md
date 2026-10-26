# Derived fixed_daily post-close D0/D1

The registered derived research account at generation-1 / 2026-10-23 was consumed by the post-close coordinator. The fixed leg produced 20 TARGET_PREVIEW targets and 20 paper orders. One candidate batch was included, and the budgeted joint projection produced 22 paper orders. StockDB close coverage was 40/40 for the combined 40 target symbols.

The coordinator reused the already committed fixed_daily head, then ran the same candidate, quote, target, order and shared Ledger projection route. No broker or Tonghuashun interaction occurred, persistent account state was not mutated by D0/D1, and xecution_admitted=false remains explicit. Full receipt: D:\Quantification\outputs\pipeline_receipts\20261027_post_close_derived_d0_d1_v1\post_close_receipt.json.
