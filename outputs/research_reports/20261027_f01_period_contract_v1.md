# F01 period contract v1

Status: `VERIFIED_LOCAL_SEMANTICS_BLOCKED_EXTERNAL_DATA`

This change applies the Google applied design decision A to the F01 pure function. Profit facts must already be `single_quarter`; cumulative facts are rejected until the explicit cumulative-to-single-quarter conversion is used. Year-over-year profit facts must share the same quarter endpoint, and the derived quarter starts on the day after the prior cumulative period ends.

Validation: `28 passed` from the focused F01, F01 signal, and F2 bridge tests. No real PIT evidence was created, no account or broker state changed, and `execution_admitted=false` remains in force.

Receipt: `outputs/pipeline_receipts/20261027_f01_period_contract_v1.json`.
