# Paper task timing binding (2026-10-27)

`paper_timing_binding.py` joins an existing post-close timing contract with one completed `stockdb_post_close_d0_d1_orchestration_v1` receipt. It requires the task to be research-only, complete, and to contain a non-negative `local_task_measurement_v1`. The output binds both source file hashes, close/execution/settlement dates, input fingerprint, coverage and paper order counts.

The binder never replays an account, contacts a provider, or treats wall time as expense. Provider cost remains `null` with status `UNMEASURED_EXTERNAL_PROVIDER_EXPENSE`; an untrusted execution flag or non-null provider cost is rejected.

Actual binding used the existing 2026-10-23 close task: 40/40 source coverage, 20 fixed paper orders and 22 joint paper orders, local wall time `0.099731` seconds. The output is `outputs/pipeline_receipts/20261027_paper_task_timing_binding_v1.json`.

Acceptance: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_paper_timing_binding.py` -> `5 passed`; `py_compile` passed. This is research-only timing evidence, not broker or Tonghuashun evidence.
