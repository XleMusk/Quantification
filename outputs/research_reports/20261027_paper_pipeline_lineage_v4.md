# Paper pipeline lineage v4

The latest 2026-10-26 fixed D0/D1 receipt is now rebuilt into the seven-stage research-only lineage. The Signal stage is derived from the actual fixed paper package and candidate manifest: `low_volatility_252_20` only, with `candidate_count=0`; no stale half-momentum strategy is inserted.

The lineage binds StockDB snapshot coverage, target holdings, paper order plans, offline THS reference payload, modeled fills and projected Cash/Position. It keeps `execution_admitted=false`, does not infer broker fills, and does not mutate a persistent account.

Receipt: `outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v4.json`.
