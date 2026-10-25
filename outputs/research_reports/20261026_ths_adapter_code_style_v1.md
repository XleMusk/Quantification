# THS adapter code-style demonstration

`trading.ths.adapter` now exposes a narrow `PrefillWindow` protocol for the external desktop boundary. The adapter's reviewed paper-only behavior remains unchanged: it validates a plan, writes only the three reviewed fields when a caller explicitly invokes prefill, reads back the values, and never submits, cancels, or persists a broker fact.

This is a structural refactor in the real THS adapter path, not a new broker integration. The protocol keeps GUI I/O behind a replaceable boundary while the business functions continue to use explicit plan, account, price and permission values.

Focused validation: **99 passed** (`test_ths.py`, `test_ths_prefill_recovery.py`, and `test_broker_sessions.py`).

The result is `VERIFIED_LOCAL`; it does not establish real THS mapping, account observation, or channel validation.
