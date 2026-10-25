# A04 decision and migration boundaries

Decision 2026-09-30: keep the existing modular application and one Ledger;
separate the actual provider/UI dependencies, not create a second platform.

Implemented: the existing symbol parser moved to `trading/symbols.py` without
changing SH/SZ aliases or rough prefix inference. `market_data.py` re-exports
the same function/class for old consumers. `trading.signals` now imports the
pure parser directly; a fresh-process check confirms that it no longer loads
the committee package or BaoStock. Prefix inference is not a verified D01
instrument identity or ETF permission.

`run_committee.py architecture-check` parses first-party Python imports,
including function-local imports. Core contracts/config/rules/ledger/engine/
symbols may import only core or the standard library. Research cannot import
THS, the xtquant adapter, old bridge, web frontend/backend or listed GUI modules.
Signals cannot initialize the committee/provider or an execution adapter.
Unresolved dynamic imports in those boundaries require review. Tests and local
virtual environments are excluded. This check is not a runtime sandbox.

| Remaining coupling | Incremental exit condition |
|---|---|
| HistoricalFeed reads data and computes rule features | Separate only when the next strategy needs shared data/feature semantics; same snapshot and ledger results must still match. |
| V1 persisted dictionaries omit new contract provenance | L01 migrates a copied account and verifies event reconstruction/balances; never overwrite a live account or assume code rollback rewinds balances. |
| THS manual-prefill layer is not the application broker channel | Real local version/mapping, account read, plan and readback are verified before linking; never enable submission by importing easytrader. |
| Old provider symbol imports | Retain aliases while actual callers use them; D01 may replace inference only with verified identity/type history and explicit adapters. |
| Futures-style bridge | Remains a separate experiment; no second writer or connection to the same A-share account. |

Result: `outputs/architecture_v2/a04_dependencies_20260930.json` passed with
54 modules and no violations. Relevant new/old parser, signal, trading and
committee integration checks passed (89 tests); final architecture-only check
passed (13). No data, account state or dependency environment was migrated.
This is a code-boundary result, not a strategy or real-client acceptance.

## 2026-10-23 reviewed boundary allowlists

The current checkout has grown beyond the original 2026-09-30 module count.
`trading/architecture.py` therefore keeps two explicit, narrow allowlists rather
than treating every `trading.*` import as safe:

- `CORE_SUPPORT` contains the internal simulation and policy helpers that the
  canonical Engine/Ledger may call: expected-return validation, lot allocation,
  entry risk, rights, event-frontier, exit lifecycle, published-close routing,
  security conversion, and the read-only research corporate-action view. The
  package-level `trading.research` import used by Engine is tracked separately
  as a one-purpose `CORE_PACKAGE_BRIDGES` entry for `order_capacity_replay`.
- `WORKBENCH_COMPOSITE` contains only `capability_gap_scan`, `opening_cycle`,
  and `trader_desk_snapshot`. These are read-only workbench composites that
  consume guarded THS observation/templates. Their membership does not grant
  broker I/O, submission, cancellation, or execution admission.

The allowlists are exact module names and are covered by
`trading/tests/test_architecture.py`. `system_daily_route_recovery` now uses
lazy explicit imports for its seven reviewed route modules, so the architecture
check records concrete dependencies instead of accepting an unresolved dynamic
import. The acceptance command is:

```powershell
.\env\python.exe -X utf8 run_committee.py architecture-check
```

This check is still static and is not runtime authorization. It does not change
the `execution_admitted=false` gate or turn a read-only composite into a broker
channel.
