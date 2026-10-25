# Quantification research-only operations runbook

This runbook operates the local research and paper pipeline described by the CTO plan. It does not log in to Tonghuashun or a broker, submit/pre-fill/cancel orders, transfer cash, buy data, or mutate a real account.

## Before a close-day run

Keep the registered fixed account under `D:\QuantificationData\trading\research\fixed_daily` and use an exact same-day source manifest or replay manifest for candidates. A replay manifest must use the registered candidate catalog and may carry a `research_decision_receipt`.

## Closed-session entrypoint

Use `run_committee.py post-close-d0-d1`. It advances the registered `fixed_daily` head when complete source packages exist, builds candidate Target inputs, captures same-day Quote data from either a live read-only StockDB service or `--stockdb-snapshot`, then emits fixed/candidate paper packages, a budgeted joint paper package and a THS read-only reference. With no registered candidates, the fixed leg still runs and the candidate batch is recorded as skipped.

Every run must use a new output directory or the exact same inputs. A changed source, snapshot, calendar, decision receipt or cash value must never overwrite a prior receipt.

## Evidence and recovery

Read `contract_receipt.json` or `post_close_receipt.json` first. `execution_admitted=false` is required. `COMPLETED_RESEARCH_ONLY_*` is a paper projection, not a broker fill. `WAITING_FIXED_DAILY_SOURCES` means the fixed source package is incomplete. `RETRYABLE_TOOL` means an infrastructure/provider observation failed; do not retry automatically or resend any unknown external action. `BUSINESS_FAILURE` means the input or business contract needs correction.

Use `run_committee.py ths-target-readback` only with an authorized read-only export. A synthetic readback is contract evidence only and never authorizes submission.

## Release boundary

`run_committee.py internal-release-candidate --root D:\Quantification --output-dir D:\QuantificationData\trading\research\releases\<version> --version <version>` writes a hash-bound local release manifest. It is an internal research release; live execution remains false until PIT data, current quotes, account exports, T+1 evidence and authorized channel observations are independently present.
