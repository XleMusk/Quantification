# THS readback provenance contract (2026-10-27)

`target_readback` now rehashes an authorized read-only export when `source_root` is supplied. Missing files and hash mismatches stop the comparison. Synthetic/offline fixtures remain explicitly unobserved and no client, submission or Ledger operation is performed.

Focused acceptance: `10 passed` in `trading/tests/test_ths_target_readback.py`. This proves the evidence contract only; it does not create real THS evidence.
