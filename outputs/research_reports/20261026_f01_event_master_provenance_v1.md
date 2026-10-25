# F01 event-master provenance v1

- Status: `BLOCKED_DATA`
- Execution: `execution_admitted=false`
- Scope: event-master provenance only; no F01 promotion

The F01 coverage funnel now propagates explicit `source_snapshot_id`, source PDF SHA256, extracted-text SHA256, and per-hash verification flags into each event-master row. The current 2024Q1 sidecar contains verified source hashes for all three rows but no source snapshot identity, so the identity remains null and the historical retrieval/as-of/revision gate remains blocked.

Validation: `17 passed` across the F01 funnel, F2 source-snapshot adapter, and F2 bridge tests. Synthetic or current retrieval evidence was not upgraded to PIT-safe evidence.
