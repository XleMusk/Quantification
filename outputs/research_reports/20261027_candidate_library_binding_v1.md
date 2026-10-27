# Candidate library binding (2026-10-27)

`candidate_library_binding.py` joins the saved candidate catalog, the latest research adoption rollup, and the six-strategy common-window panel by the catalog `candidate_spec_id`. It records the panel strategy aliases, shared spec variants such as the Ridge horizon pair, current research action, comparability group, and source evidence hashes.

Shortline, public-seat, value-size, and leader-style entries remain explicit `SEPARATE_GATE_BOUND` records. They are not treated as common-window comparisons. The binding is read-only and does not rerun strategies or alter adoption decisions.

The actual v10 binding contains 9 catalog entries: 6 common-panel decisions and 4 separate gates, with the Ridge entry carrying two panel variants. All flags remain `execution_admitted=false`, `strategy_validated=false`, `paper_allowed=false`, and `promotion_allowed=false`.

Acceptance: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_candidate_library_binding.py` -> `2 passed`; `py_compile` passed. CLI output: `outputs/pipeline_receipts/20261027_candidate_library_binding_v1.json`.
