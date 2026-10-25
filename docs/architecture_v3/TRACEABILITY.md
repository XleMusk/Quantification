# V3 implementation traceability

This file binds the 2026-10-23 V3 reconstruction specification to the
current checkout. It is an implementation control record, not a completion
claim. The source document remains the user's supplied DOCX; its historical
statement that the document itself did not modify the project is provenance,
not a restriction on the user's later request to implement the plan.

## Source and current scope

- Source: `C:\Users\13944\Downloads\quant_rebuild_V3_detailed_20261023.docx`
- Extracted source structure: 390 paragraphs, 10 tables, no tracked changes,
  comments, or embedded media.
- Current repository: `D:\Quantification`, branch `main`.
- Current execution gate: `execution_admitted=false`.
- Prohibited actions for this implementation: real broker login, order,
  cancellation, transfer, payment or paid data purchase.
- A local read-only data service, synthetic adapter, paper ledger and offline
  template do not prove a live channel or account result.

## V3 completion states

Every requirement is evaluated in a bounded scope and version:

`CODE_INTEGRATED` → `VERIFIED_LOCAL` → `DATA_READY` → `MARKET_EVALUATED`
→ `ENVIRONMENT_VALIDATED` → `CHANNEL_VALIDATED`.

The states are not interchangeable. A synthetic test may establish
`VERIFIED_LOCAL`; it cannot establish `DATA_READY`, `MARKET_EVALUATED`, or a
channel state. `BLOCKED_DATA`, `NO_OPPORTUNITY`, and `NOT_APPLICABLE` remain
explicit outcomes and are never rewritten as green completion.

## Four business slices that control implementation order

1. A real source event enters a timepoint snapshot, produces a fixed signal,
   and explains inclusion or exclusion.
2. The signal passes through target allocation into the shared simulation
   account with fees, blocked orders and expiry handling.
3. Duplicate, out-of-order and interrupted facts are replayed safely, and the
   browser can trace a result back to its source.
4. A frozen candidate continues on new dates with coexisting versions,
   recoverable publications and explicit missing inputs.

Each slice must cross data, research, portfolio, ledger, operations and UI
boundaries. A module-only test is supporting evidence, not slice completion.

## Current binding observed before V3 implementation

The V2 baseline confirms the real paths are already present for the shared
Engine/Ledger replay and paper core, typed contracts, simulated broker
adapter, and guarded THS adapter. It also records the limits that V3 must
close or preserve: StockDB is still a read-only audit source; durable event
provenance and account-version migration remain open; the THS native four-table
package, account observation, GUI field mapping, same-session quotes and real
readback remain absent; research comparisons remain diagnostic-only.

The first implementation pass therefore binds the existing paths and evidence
before changing public semantics. It does not reset old accounts, rewrite old
research, or silently replace unsupported instruments with main-board names.

## V3 work package binding

The machine-readable task register is
`docs/architecture_v3/work_packages_v3_binding.json`. Each F/D/R/L/P/E/O/U
package retains its V2 identifier and records:

- the V3 business result that must be produced;
- preparation dependencies versus acceptance dependencies;
- the current evidence category;
- the next concrete implementation action;
- the prohibition that prevents a weaker artifact from being counted as done.

The initial register intentionally marks unbound work as `UNBOUND` or
`PARTIAL`. It is updated only when a real file, command, runtime observation
or bounded research receipt supports the new state.

The first ledger evidence is recorded in
`docs/architecture_v3/L01_L03_REFERENCE.json` and exercised by
`trading/tests/test_v3_ledger_reference.py`. It is a local synthetic
`VERIFIED_LOCAL` result for L01 and L03: the hand calculations cover cash,
holdings, fees, T+1, duplicate fills, entitlement receivables and payment.
It does not establish a real account, broker export or channel result.

The financial-factor extension keeps its original FP01–FP24 identifiers in
`docs/architecture_v3/f2_work_packages_binding.json` and its implementation
boundary in `docs/architecture_v3/F2_TRACEABILITY.md`. F2 does not create a
second platform or account path; it binds financial facts and point-in-time
semantics to the existing factor and Engine/Ledger routes.

The 2026-10-22 GitHub review and implementation plan is bound separately in
`docs/architecture_v3/github_plan_20261022_binding.json`, with the short
current status in `docs/architecture_v3/CURRENT_STATUS.md`. The source's
historical branch/PR observations are retained as provenance and are never
silently promoted to current Git or channel evidence. The first newly closed
P0 slice is the local cancel-race invariant: a partial late fill preserves
`cancel_pending`, while a complete fill wins and becomes `filled`. The local
offline candidate check and artifact-bundle empty-directory restore are also
available, but the full integration candidate, persistent browser worker and
complete configuration/account restore remain open.

## Acceptance rule for this reconstruction

The V3 plan is not complete until every required slice has a versioned input,
reproducible output, failure and recovery behavior, and the applicable state
above. A complete internal engineering version may still remain blocked on
external data or channel conditions. That limitation must be reported beside
the result, while independent engineering and research work continues.

Model/service interruptions are governed by
`docs/architecture_v3/GOAL_CONTINUITY.md` and
`docs/architecture_v3/goal_continuity.json`. A capacity error is
`RETRYABLE_INFRA`, so it preserves the goal and work package state; it cannot
be recorded as a business failure or completion.
