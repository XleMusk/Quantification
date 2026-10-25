# F2 source-snapshot provenance v1

The historical gate now preserves `source_snapshot_id`, source file path, source SHA256 and the source-hash verification flag. The fact bridge carries these fields on each materialized fact without weakening the PIT gate.

Validation: F2 historical gate, source snapshot adapter and fact bridge tests — `15 passed`. The fixture is synthetic; locked CNINFO 2023Q1 evidence remains `BLOCKED_DATA`.
