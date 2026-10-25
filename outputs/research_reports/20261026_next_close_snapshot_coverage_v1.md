# Next-close snapshot coverage v1

The next-close contract now records explicit StockDB date and symbol coverage. A pinned snapshot whose effective session dates omit the requested close date is `STALE_SNAPSHOT`; a snapshot with the date but missing requested securities is `INCOMPLETE`. Neither path carries a price forward or produces an execution-ready result.

Validation: next-close and post-close focused tests — `25 passed`.
