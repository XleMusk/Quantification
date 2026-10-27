# Research adoption rollup v12

The no-argument `research-adoption-rollup` command now binds the latest fixed D0/D1 receipt, current-window comparison, full six-strategy panel and candidate freshness gate by default. The generated receipt records close `2026-10-26`, fixed target/order evidence and `candidate_count=0`; the candidate gate remains `BLOCKED_STALE_CANDIDATE_INPUT`.

All candidates remain research-only and promotion disabled.

Receipt: `outputs/pipeline_receipts/20261027_research_adoption_rollup_v12.json`.
