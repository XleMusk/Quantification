# F2 bridge revalidation (2026-10-26)

## Delivered

`trading/research/f2_fact_bridge.py` now recomputes the publication contract from
each sidecar row when a decision day is present. The bridge no longer trusts a
cached `contract_evaluation.pit_asof_safe=true` flag without row-level
`retrieved_at`, `asof_at`, `revision_id`, and source provenance evidence.

## Verification

- Focused F2, publication-contract, and F01 tests: **15 passed**.
- Replayed the saved three-row issuer sidecar with the revised bridge.
- Receipt: `outputs/pipeline_receipts/20261026_f2_bridge_revalidated_v1.json`.
- Result remains `BLOCKED_DATA`: 3 rows blocked, 0 facts materialized, 0 factor
  values, 0 account actions.
- Existing failure reasons remain explicit: late retrieval/as-of evidence and
  missing issuer scope. No rows were upgraded by this change.

## Boundary

This closes a semantic bypass in the local bridge. It does not create historical
PIT evidence for the locked 2023Q1 sample, and it does not permit F2 to enter
Signal, Target Portfolio, Orders, Fill, or Cash/Position until the original
publication, retrieval/as-of, revision, unit, and scope evidence is complete.
