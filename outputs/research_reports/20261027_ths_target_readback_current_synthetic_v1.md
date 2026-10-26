# Current D0/D1 THS readback contract fixture

The current 2026-10-23 fixed TARGET_PREVIEW target set was converted into a 20-row THS read-only reference and a matching synthetic readback. The comparator returned `readback_verified`; all rows matched.

This proves the reusable comparison contract and preserves `account_observed=false`, `quote_observed=false`, `client_interacted=false`, `submitted=false`, and `execution_admitted=false`. It is a synthetic fixture generated from the paper target, not an observation of a THS client window.
