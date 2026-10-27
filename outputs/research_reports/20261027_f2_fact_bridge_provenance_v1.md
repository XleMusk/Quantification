# F2 source snapshot provenance in fact bridge (2026-10-27)

The F2 fact bridge now carries `source_snapshot_id`, `source_sha256`, `snapshot_source_check`, and `snapshot_source_check_passed` from the verified source snapshot into every row's `source_provenance`. A mismatched source hash remains blocked; the change only exposes provenance and does not relax publication, retrieval/as-of, revision, scope, or unit gates.

A real 18-row bridge rerun remains `BLOCKED_DATA`: 0 ready facts and 0 factor rows. The existing 000002.SZSE Wayback evidence is after the decision cutoff and still lacks revision-safe PIT evidence. Focused bridge, source snapshot, historical evidence, and vertical-slice tests passed (`27 passed`). Commit: `da312f5`.

The next required input is a real pre-decision source snapshot. This package makes the evidence traceable once that input exists; it does not manufacture one.
