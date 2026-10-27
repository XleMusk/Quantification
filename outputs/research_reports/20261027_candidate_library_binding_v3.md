# Candidate library THS readback binding (2026-10-27)

Candidate library v3 adds one channel binding to the saved TARGET_PREVIEW THS readback comparison. The binding records source hash, comparison status, row count and an explicit observation class. The current source is `READBACK_VERIFIED_SYNTHETIC_ONLY`, so `authorized_readonly_observation=false`; no client interaction, submission, ledger mutation or execution admission is inferred.

The library still joins existing evidence only. It does not open Tonghuashun, prefill a client, submit an order, or convert synthetic rows into channel evidence. The v3 default output is `outputs/pipeline_receipts/20261027_candidate_library_binding_v3.json`; focused tests passed (`3 passed`).
