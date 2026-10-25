# Candidate batch TARGET_PREVIEW boundary

The candidate fan-in now propagates `TARGET_PREVIEW` and `ephemeral_projection` from each shared paper package into both candidate rows and the batch receipt. The batch explicitly remains outside the persistent `fixed_daily` account and cannot publish a frozen plan or continuous paper rollover.

Evidence is a synthetic local fixture with two candidates; focused tests: **6 passed**.
