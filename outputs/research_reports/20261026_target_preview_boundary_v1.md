# TARGET_PREVIEW boundary

- `stockdb_paper_target_package` now accepts only `TARGET_PREVIEW`.
- Its local fills are explicitly `observed_quote_projection` on an `ephemeral_projection` Ledger.
- The result cannot be read as a published D0 plan or `ONLINE_PAPER` rollover; persistent account id is null and no Ledger is persisted.
- Focused tests: **12 passed**.

The next real close must use the fixed_daily/post_close D0/D1 consumer for continuity.
