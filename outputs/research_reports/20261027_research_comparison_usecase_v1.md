# Research comparison application use case v1

Status: `VERIFIED_LOCAL_READONLY_USECASE`

The saved same-sample strategy comparison now has an explicit application view in `trading/research/research_comparison_view.py`. The HTTP routes only parse the request, map errors, and delegate the comparison window or saved snapshot catalog. Existing response fields and read-only behavior are preserved.

Validation: `20 passed` across the new view tests and the existing API/reader tests. The consumer test confirms the endpoint does not use account storage or mutate saved source artifacts. No strategy promotion, paper ledger mutation, broker access, or THS interaction occurred; `execution_admitted=false` remains in force.

Receipt: `outputs/pipeline_receipts/20261027_research_comparison_usecase_v1.json`.
