# Weekly research review v3

Staged post-close, F2 source selection, six-strategy comparison and local workbench evidence advanced. Full-universe close and real PIT/THS gates remain closed.

- Six-strategy common window: `2026-01-05..2026-10-13`, 184 sessions, all six `STOP_RESEARCH_ONLY`.
- Full-market 2026-10-26 StockDB coverage: 226/5211 rows, 4985 missing; no imputation.
- Explicit fixed target subset: 20/20 symbols complete, research-only.
- F2 raw snapshot bridge: `BLOCKED_DATA`, 18 rows, 0 ready facts.
- Workbench HTTP chain: `COMPLETED_LOCAL_RESEARCH_HTTP_CHAIN`, same job id `True`, downloads `True`.
- THS: synthetic readback only; no authorized client observation.

## Blockers

- D0: complete full-universe close package missing.
- F2: pre-decision retrieval/as-of/revision evidence missing.
- T1: authorized THS read-only export missing.

## Next route

Use the staged coordinator after the next close, keep missing rows explicit, and require PIT plus untouched-forward evidence before strategy adoption.
