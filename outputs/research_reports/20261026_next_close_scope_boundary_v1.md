# Next-close account scope boundary

`next_close_d0_d1` and `post_close_d0_d1` now state the persistent `fixed_daily` account scope separately from the fixed/candidate `TARGET_PREVIEW` projections. Candidate and fixed projections remain ephemeral and cannot claim to be the continuous paper account.

Focused tests: **22 passed**. The receipt is local contract evidence; it does not claim that the 2026-10-26 real close has already been consumed.
