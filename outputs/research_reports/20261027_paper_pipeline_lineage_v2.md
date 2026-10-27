# Reproducible paper pipeline lineage CLI (2026-10-27)

`paper_pipeline_lineage.py` rebuilds a research-only Data-to-Cash/Position lineage from saved post-close, fixed, joint paper and THS readback receipts. It validates the execution boundaries and hashes each source file before emitting stage records for StockDB data, strategy signal inputs, TARGET_PREVIEW, paper orders, offline THS reference payloads, modeled paper fills, and projected cash/positions.

The BrokerAdapter stage is explicitly `REFERENCE_PAYLOAD_ONLY`; the Fill stage is `MODELED_PAPER_FILL_PROJECTION` with an unknown count rather than an inferred count. The CLI does not rerun strategies or access external channels.

Acceptance: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_paper_pipeline_lineage.py` -> `2 passed`; `py_compile` passed; default CLI produced `outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v2.json`.
