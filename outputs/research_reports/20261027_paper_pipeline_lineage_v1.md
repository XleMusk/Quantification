# Paper pipeline lineage (2026-10-27)

This receipt binds the saved 2026-10-23 close package through the research-only paper path: StockDB data and candidate manifest, two strategy signals, fixed and joint TARGET_PREVIEW targets, paper order plans, offline THS reference payloads, modeled paper fills, and projected cash/positions. Every source file is hash-bound.

The BrokerAdapter stage is explicitly `REFERENCE_PAYLOAD_ONLY`; no client interaction or submission occurred. The Fill stage is `MODELED_PAPER_FILL_PROJECTION` and leaves count unknown instead of inferring one from order count. The Cash/Position stage reads the saved projected account: initial cash CNY 140,000, modeled-fill cash CNY 78,534.34, marked equity CNY 139,889.34, 22 positions.

This is local research-only evidence, not a broker fill or live account record.
