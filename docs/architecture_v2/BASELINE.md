# V2 implementation baseline (2026-09-27)

The V2 goal is an internal A-share research-to-paper platform with explicit data,
decision, account, and execution boundaries. Tonghuashun is a possible later
terminal, not the system architecture or an authorization to trade. This file
records code that exists, not capabilities inferred from the V2 proposal.

## A01: actual paths and state owners

| Entry or boundary | Current path | State writer / authority |
|---|---|---|
| CLI and web UI | `run_committee.py`; `webapp/backend/app/main.py` mounts `trading/api.py` | The API validates requests and calls `trading/store.py`; the UI does not own simulation cash or positions. |
| Replay and paper runs | `trading/api.py:create_run` -> `trading/store.py:create` -> `trading/worker.py:step` -> `trading/engine.py:advance` | `runs.state` plus `daily` in `D:\QuantificationData\trading\trading.sqlite3` (or `QUANT_TRADING_HOME`). `worker.step` advances and commits one daily state; `store` creates/controls runs. |
| Fixed-rule research replay | `run_committee.py market-replay` -> `trading/research/market_replay.py:run` -> `trading/research/experiment.py:replay` -> `engine.advance` | Uses the same engine and `Ledger`, but writes a separate in-memory research account to `D:\QuantificationData\trading\research\market_replay`; it is not a second writer of `runs.state`. |
| Model/factor replay | `run_committee.py factor-mine` -> `trading/research/experiment.py` (`ScoreFeed`, `replay`) -> `engine.advance` | Model scores enter before target construction. Missing model-day batches now produce `SignalUnavailable`, not an empty liquidation target. |
| Data acquisition and normalization | `trading/market_dataset.py` (BaoStock cache), `trading/stockdb_source.py` (read-only audit), `trading/market_snapshot.py`, `trading/feed.py` | Source caches and snapshot files are external. `HistoricalFeed` validates and reads an admitted snapshot; StockDB has not been promoted to the trading feed. |
| Target, order, fill, account | `trading/engine.py:choose_targets/advance/simulate_order`; `trading/ledger.py:Ledger`; `trading/rules.py` | `Ledger` mutates simulation cash, positions, orders, and fills. `engine` builds targets and next-session orders. `trading/contracts.py` now distinguishes `FULL_TARGET`, `DELTA`, and `NO_CHANGE` at the signal/portfolio boundary. |
| Tonghuashun workbench | `trading/ths/cli.py`, `adapter.py`, `observations.py`, `journal.py` | Separate `ths_*` plans/observations. It can use a temporary `Ledger` for plan checks, but it does not write `runs.state` or submit real orders; see `trading/ths/README.md`. |
| Older vn.py bridge and Qlib | `bridge/vnpy_strategy.py`; older Qlib work described in `docs/A_SHARE_QUANT_FOUNDATION.md` | Separate experimental paths, not account writers for the new `trading` ledger. Do not silently connect them to the same account. |

`HistoricalFeed` currently combines snapshot reading, feature calculation and rule
scores; `engine.advance` directly expects its rows, calendar, instrument status,
actions, volume and scores. This is the main data/strategy coupling to separate
incrementally. Replay pins a dataset version; paper uses a changing default
dataset path. A database run marked `running` is not proof a worker is online;
check `store.health()` and the cursor/daily records separately. Historical
membership, source field semantics, broker UI mapping, real account state and
execution authority remain unverified where their own evidence is absent.

## A02: fixed behavior and reference

Machine-readable input/output index: `outputs/architecture_v2/baseline_manifest.json`.
Three existing 10-stock SQLite replays, one per rule signal, were reproduced
read-only against their pinned snapshot. Each matched 663/663 daily records and
the final account state before and after the intent change. The old daily
records omit the later-added `dataset` metadata on all 663 days; the reproducer
compares this field when it exists and reports the omission explicitly. Original
database rows and exported results were not overwritten. Run-specific reports
contain code/input SHA256, configuration, environment and first-difference data.

For a small fixed-250 reference, the first five members in the supplier's
2023-12-29 main-board list were inspected on the same 2024-01-02..04 dates
(15 unadjusted daily bars, all trading and non-ST). The manifest pins source
hashes. `test_fixed_250_member_hand_calculated_cash_and_T1` checks a 100-share
600000.SSE round trip using the source's 2024-01-03/04 opens: 6.59/6.64 RMB.
At 10 bps slippage, fill prices are 6.60/6.63. Buy fees are 5.01 RMB;
sell fees are 5.34 RMB. Starting with 10,000 RMB, cash after buy is 9,334.99,
same-day sale is rejected by T+1, and cash after next-day sale is 9,992.65.
This is a ledger arithmetic reference, not a strategy backtest or proof of
source-wide quality. The 250-name source files report complete, but a validated
fixed-250 snapshot and independent membership check remain outstanding.

Reproduce a stored run without mutating its SQLite rows:

```powershell
.\env\python.exe -X utf8 run_committee.py reproduce-run --run-id 422cbdb8e78848dfad07375f46c28e67
```

## A03: versioned core contracts (2026-09-30)

`trading/contracts.py` now defines `RunContext`, `SecurityId`, `Price`,
`ShareQuantity`, `SignalTime`, `Signal`, `SignalFrame`, `PortfolioIntent`,
`OrderIntent` and simulated-fill `ExecutionEvent`. Their JSON readers validate
`schema_version=1`, object type, units and nested contracts; unknown versions
are rejected. See `CORE_CONTRACTS.md` for semantics and compatibility limits.

`HistoricalFeed.signal_frame` and `ScoreFeed.signal_frame` produce typed score
batches. `engine.advance` validates the batch before session mutation and uses
the same portfolio path. `simulate_order` sends typed orders and fills to
`Ledger.submit_intent/apply_execution`. Worker runs propagate their account ID
and replay/paper mode. Proxy trend feeds retain their own scores rather than
accidentally invoking the base rule feed. Missing model/trend batches mean
`NO_CHANGE`; a valid explicit empty full target still means exit next session.
`DELTA` remains a represented weight change, rejected by the V1 executor.

The V1 SQLite state and old list/dictionary entry points remain unchanged through
compatibility adapters. No account database migration occurred; new transient
context/time/source fields are not backfilled into old records. Durable event
provenance and account-version migration remain L01/O02 work, not an A03 result.
Security code references are not D01 permanent instrument identities or proof
of ETF/BSE trading support. Historical EOD availability assumptions remain
explicit, with unknown publication/receipt/computation times left unknown.

Verification: 192 trading/research tests passed. Each of the three pinned old
replays still matches all 663 daily records and its complete final state.
Reports: `outputs/architecture_v2/a03_20260930_<run_id>.json`; work-package result:
`outputs/architecture_v2/A03_RESULT.md`. These are compatibility checks, not new
strategy evidence or paper/live execution.

## Next dependency boundary

A04 has implemented source import checks and separated pure symbol parsing;
see `DEPENDENCY_BOUNDARIES.md`. The user's updated practical dual-track goal
now sets priority through `IMPLEMENTATION_QUEUE.md`, rather than mechanically
following the full topological order. Before admitting new data, audit source
membership, raw/adjusted/reference-close and action semantics against a
validated snapshot. R01/R02 depend on those data contracts; E01 should state
real THS capabilities and unknown results separately before any adapter work.
No live login, submission, cancellation or transfer is authorized by V2.

## Shared broker consumer (2026-09-30)

`trading/broker.py` defines the common execution/read surface. The engine now
routes its simulated order/fill/cancellation through `SimulatedBrokerAdapter`,
which delegates to the same Ledger, without a new account or persistence layer.
`THSAdapter` composes existing guarded capture, absolute-target planning and
three-field readback. The existing THS CLI consumes it; THS submit/cancel always
deny. Capabilities describe code, not verified windows or account authority.
All three pinned 663-session old replays remain identical, including final
state (`outputs/architecture_v2/broker_20260930_<run_id>.json`). This does not
complete actual-client integration, a current strategy feed, or live readiness.
