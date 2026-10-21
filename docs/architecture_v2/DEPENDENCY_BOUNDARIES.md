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
