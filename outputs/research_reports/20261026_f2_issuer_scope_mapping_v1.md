# F2 issuer scope and period mapping

Status: `BLOCKED_DATA`; `scope_verified=True`.

The locked 000002.SZSE/2023Q1 report now has explicit field semantics:

| Field | Scope | Period kind | Statement role | Unit | Currency |
|---|---|---|---|---|---|
| `parent_net_profit` | `parent_attributable` | `single_quarter` | `flow` | `yuan` | `CNY` |
| `total_assets` | `consolidated_group` | `instant` | `stock` | `yuan` | `CNY` |

The report wording and candidate metadata support the mapping. The receipt remains `BLOCKED_DATA` because the current retrieval cannot prove historical retrieval/as-of availability at the 2024-05-01 decision cutoff or revision history. No factor, target, order, fill or account action was created.
