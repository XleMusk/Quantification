# Core contracts v1

A03 implementation, 2026-09-30. The implementation is
`trading/contracts.py`, independent of pandas, Qlib, broker SDKs and GUI code.
This document describes implemented semantics, not the full V2 destination.

## Object and unit map

| Object | Meaning / constraint |
|---|---|
| `RunContext` | Simulation account ID plus `historical_replay` or `end_of_day_paper`. No live mode is accepted. |
| `SecurityId` | Canonical six-digit string plus SSE/SZSE/BSE; leading zeros retained. Not a permanent instrument identity, asset-type classification or account permission. |
| `Price` | Positive integer CNY cents (legacy) or explicitly declared mills (0.001 RMB), with raw/forward-adjusted/backward-adjusted basis. Order/fill consumers only accept raw and the reviewed instrument tick. |
| `ShareQuantity` | Positive integer shares, never lots or a money amount. Ledger retains lot, cash and T+1 checks. |
| `SignalTime` | Market event, availability, optional publication/receipt/computation times, availability evidence. All known times require UTC offsets. |
| `Signal` | Finite ranking score or expected-return fraction; the latter requires a session horizon. Diagnostics remain separate from the signal value. |
| `SignalFrame` | One EOD session's immutable, unique-security signals, source and run context. Availability must be no later than 15:05 China time. |
| `PortfolioIntent` | FULL_TARGET / DELTA / NO_CHANGE. Values are fractions of account equity, not shares or RMB. New decisions may carry context, decision time, earliest execution day and source. |
| `OrderIntent` | Account/mode, stable request ID, session, security, buy/sell, shares, raw CNY limit price and reason. |
| `ExecutionEvent` | Simulator fill ID, order ID, account/mode, security/side, session, shares, raw price and later settlement day. Not a general broker correction/cancel event. |

Every `to_dict()` includes `schema_version=1` and `contract_type`, except the
explicit mill-valued `Price` addition, which uses `schema_version=2`. Nested prices
and quantities declare their units. Matching `from_dict()` methods validate the
version and meaning; they do not silently reinterpret yuan, lots, foreign
currency, market orders or future schema versions.

## Actual consumers

1. `HistoricalFeed.signal_frame` wraps the existing rule calculation; its
   legacy `scores` output remains available. Raw execution bars and adjusted
   feature bars are still validated by `HistoricalFeed`.
2. `signals.frame_from_qlib` retains each sorted security's supplied availability
   timestamp. `from_qlib` remains the ranked-dictionary compatibility entry.
   `ScoreFeed` caches typed batches and explicitly returns `SignalUnavailable`
   when a model-day batch is absent.
3. `engine.advance` checks batch session/context and score semantics before
   modifying the account. Expected returns are not automatically ranking
   scores: they need a separately defined portfolio policy, not implemented
   here. Legacy ranked-list feeds use a named adapter. A proxy cannot silently
   substitute its base feed's rule scores for its own model/trend scores.
4. `simulate_order` constructs typed orders and fill events;
   `Ledger.submit_intent/apply_execution` checks account/mode and security/side
   before calling the existing single ledger. Explicit adjusted quotes fail.
5. `worker.step` supplies the persisted run ID and its replay/paper mode. No new
   account writer or brokerage adapter is introduced.

## Time and missing-decision policy

Daily bar signals identify the session close (15:00 China time) as event time.
Legacy rule/list feeds do not prove publication, receipt or computation times;
those fields stay null. The 15:05 historical availability upper-bound assumption
is labeled `historical_eod_assumption`, not fabricated source evidence. A model
timestamp is `provided`, meaning supplied by the caller, not independently
observed. This change does not establish point-in-time or prospective data.

`None` pending target / unavailable model or trend batch means no new decision.
A valid ranked batch with zero eligible securities generates an **explicit empty
FULL_TARGET**, which does liquidate on the next eligible session. An omitted
security in a full target has target weight zero. DELTA currently describes
signed equity-weight changes only and cannot be executed by the V1 account path.

## Compatibility and migration

Existing `Ledger.submit/fill`, feed `scores`, model `from_qlib`, and persisted
`None`/weight dictionaries remain compatible. Persisted V1 cash, order prices,
fill prices and fees remain integer cents; position/fill quantities remain
shares. No new schema fields or inferred historical context are written into
old SQLite rows. The unchanged legacy account format permits code rollback
without a reverse data migration for this work package; it does not justify
rewinding an account that has subsequently advanced.

New context/time/source metadata currently exists in typed contracts, not the
V1 stored event stream. Legacy fill deduplication still uses its original stored
signature, which does not include settlement provenance. Durable event replay,
broker namespaces, corrections and account-state versions need L01; release
and database rollback need O02. Stable identities and asset permissions need
D01. Contracts alone do not admit StockDB, mixed ETFs/BSE or a live account.

Verification and report links are in `outputs/architecture_v2/A03_RESULT.md`.

## Explicit fund quote extension (2026-09-30)

The existing cent price constructor and its V1 wire/state format remain
unchanged for stocks. New mill prices use `Price.from_mills/from_rmb` with
explicit units; off-tick source prices fail instead of rounding. Money remains
integer cents. Fund orders retain cumulative mill notional and round its cash
total once, so splitting a fill callback cannot change fees or cash.
Only an explicitly supplied, reviewed 510300 secondary-market T+1 profile in
2024-01-01 through 2026-09-30 is implemented. Unknown funds, dates or absent
profiles fail closed. Mixed-unit states record `price_units` and require the
same profile on resume; old code is not a supported reader for these new states.
No old account is migrated or made a mixed account by this extension.
The synthetic mixed Engine fixture tests accounting, not historical ETF
admission. Actual cached quote diagnostics and remaining data gaps are in
`outputs/architecture_v2/QUOTE_PRECISION_RESULT.md`.
