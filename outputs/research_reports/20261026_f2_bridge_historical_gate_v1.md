# F2 bridge historical evidence gate

The bridge now consumes a provider-neutral historical evidence receipt. Current network fetches and rows without a matching historical gate are rejected before FinancialFact materialization.

The current run is `BLOCKED_DATA` with 3 rows, 0 facts, 0 factor computation and 0 account replay. The existing sidecar rows do not have a matching `HISTORICAL_EVIDENCE_READY` row, so no downstream action is created. A ready historical archive/provider revision row can pass the gate only after publication, historical retrieved/as-of, revision and source provenance checks all pass.
