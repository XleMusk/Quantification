# Engine action-binding boundary

The Engine and intraday session now depend on the small core-support module `trading.corporate_action_binding` for the once-only account binding check. The research module keeps loading and interpreting the pinned evidence and exposes a versioned marker; it is no longer imported by the account consumers for this check.

This is a structural code-style demonstration. It preserves the existing replay-only validation, Ledger behavior, strategy rules and `execution_admitted=false`. It does not promote corporate-action evidence to PIT-safe data.

Focused validation: **26 passed** (architecture audit, boundary tests and the existing research corporate-action suite).
