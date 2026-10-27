# Candidate library lineage binding v5

The candidate library now binds the latest adoption rollup `v11`, which includes the completed 2026-10-26 fixed D0/D1 receipt, the six-strategy common-window panel, the current-window diagnostic and the candidate same-day freshness gate.

The latest D0/D1 gate is complete for 20/20 fixed target symbols with 20 fixed paper orders and 20 joint paper orders, while the same-day candidate gate remains `BLOCKED_STALE_CANDIDATE_INPUT` with one stale candidate. The library contains 9 catalog entries; adoption summary is {'continue_research_only': 3, 'stop_research_only': 2, 'blocked_coverage': 1, 'unassessed_separate_gate': 4, 'strategy_validated': 0}.

All rows remain research-only: `execution_admitted=false`, `strategy_validated=false`, `paper_allowed=false`, and `promotion_allowed=false`. No strategy was rerun and no broker or THS channel was accessed.

Receipt: `outputs/pipeline_receipts/20261027_candidate_library_binding_v5.json`.
