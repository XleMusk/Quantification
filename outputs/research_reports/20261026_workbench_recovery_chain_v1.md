# Workbench recovery chain

Local paper-job recovery now exposes a read-only lineage for a parent and all explicitly resubmitted children.  Recovery attempts are monotonic (`0 → 1 → 2`), and a GET does not mutate queue state.

The focused backend suite passed (`28 passed`).  The chain is research-only: automatic retry, broker/Tonghuashun interaction, order actions and account mutation remain disabled.

The receipt is a local engineering fixture derived from `test_recovery_chain_is_monotonic_and_read_only`; it is not a current-market or account observation.
