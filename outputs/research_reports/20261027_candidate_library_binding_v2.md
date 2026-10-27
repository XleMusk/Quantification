# Candidate library pipeline binding (2026-10-27)

The candidate-library receipt now exposes the saved pipeline inputs alongside the catalog and strategy decisions: the latest D0/D1 paper receipt, candidate same-day gate, full-panel comparison, and current-window comparison. These are source-hash references only; the binder does not rerun a strategy, rewrite a target, or change an adoption decision.

The default v2 output still contains 9 catalog entries, 6 common-panel strategy decisions (with the two Ridge horizons as a shared spec variant), and 4 separate gates for value-size, shortline, public-seat, and leader-style candidates. All execution, validation, paper, and promotion flags remain false.

Acceptance: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_candidate_library_binding.py` -> `2 passed`; CLI output: `outputs/pipeline_receipts/20261027_candidate_library_binding_v2.json`.
