# Paper account continuity use case (2026-10-27)

The `/paper-account-continuity` route now delegates to `trading/research/paper_account_continuity.py`. The use case receives an explicit project root, locates the latest saved continuity receipt, and returns the existing read-only view contract. It does not read the environment or clock while projecting the result and it cannot advance the account or Ledger.

The saved, missing, and invalid receipt cases are covered directly. The continuity/API/operator/architecture slice passed 53 related tests; the combined changed-slice run passed 89 tests. Commit: `e36a4b8`.

This is a local paper view, not a broker or Tonghuashun observation. The next use-case extraction must be driven by a real caller and must reduce caller knowledge.
